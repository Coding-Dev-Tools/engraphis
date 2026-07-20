"""Server-side sync-relay gate: only a valid, unexpired, non-revoked, sync-plan license
can push or pull, and accounts are isolated. Also exercises the client RelayTransport
end-to-end against the real endpoints via a TestClient shim.

Runs on the numpy-only gate: stdlib + fastapi TestClient, no network.
"""
import base64
import io
import json
import time
import threading
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import licensing
from engraphis.backends import sync_relay as relay_backend
from engraphis.licensing import LicenseError, ed25519_public_key
from engraphis.inspector import license_cloud
from engraphis.inspector import license_registry as reg
from engraphis.inspector import sync_relay
from engraphis.inspector.auth import AuthStore
from engraphis.backends.sync_relay import RelayError, RelayTransport, RelayUnreachable
from engraphis.core.store import Store
from engraphis.core.sync import SYNC_FORMAT, SyncEngine

SECRET = bytes(range(32))  # deterministic test vendor keypair


@pytest.fixture(autouse=True)
def _relay_env(monkeypatch, tmp_path):
    # verify against the test keypair (conftest already sets _TEST_MODE_PUBKEY_OVERRIDE)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    # _authorize uses the relay's OWN per-IP burst budget (_relay_rate_ok), separate from
    # /license/v1/register's. Every TestClient request in this file arrives from the same
    # synthetic peer ("testclient"), so leave the limiter effectively off by default and
    # let the tests that are ABOUT it set their own budget — otherwise the suite throttles
    # itself, not the attacker. Clear both buckets so neither leaks across tests.
    monkeypatch.setattr(license_cloud, "RELAY_RATE_PER_MINUTE", 10_000)
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 10_000)
    license_cloud._RELAY_BUCKETS.clear()
    license_cloud._REGISTER_BUCKETS.clear()
    yield
    license_cloud._RELAY_BUCKETS.clear()
    license_cloud._REGISTER_BUCKETS.clear()


def _key(plan="pro", email="buyer@example.com", *, expires_in_days=30,
         subscription_id=""):
    now = time.time()
    exp = None if expires_in_days is None else int(now + expires_in_days * 86400)
    payload = {"v": 1, "plan": plan, "email": email, "seats": 1,
               "issued": int(now), "expires": exp}
    if subscription_id:
        payload["subscription_id"] = subscription_id
    return licensing.compose_key(payload, SECRET)


def _app(*, service=None):
    app = FastAPI()
    if service is not None:
        app.state.service = service
    app.include_router(sync_relay.router)

    @app.exception_handler(LicenseError)
    async def _license(request, exc):
        return JSONResponse({"error": str(exc), "feature": getattr(exc, "feature", None)},
                            status_code=402)

    return TestClient(app)


# ── server-side gate ─────────────────────────────────────────────────────────────────

def _auth(key):
    return {"Authorization": "Bearer %s" % key}


def test_valid_pro_key_can_push_and_pull():
    c = _app()
    r = c.post("/relay/v1/ws1/bundles/bundle-devA.json",
               content=b'{"hello":1}', headers=_auth(_key()))
    assert r.status_code == 200 and r.json()["bytes"] == 11
    got = c.get("/relay/v1/ws1/bundles", headers=_auth(_key()))
    assert got.status_code == 200
    bundles = got.json()["bundles"]
    assert len(bundles) == 1 and bundles[0]["name"] == "bundle-devA.json"
    assert base64.b64decode(bundles[0]["data"]) == b'{"hello":1}'


def test_bearer_scheme_is_case_insensitive():
    c = _app()
    key = _key()
    response = c.get(
        "/relay/v1/ws1/names",
        headers={"Authorization": "bearer %s" % key},
    )
    assert response.status_code == 200


def test_missing_key_is_rejected():
    c = _app()
    assert c.get("/relay/v1/ws1/bundles").status_code == 402
    assert c.post("/relay/v1/ws1/bundles/x.json", content=b"{}").status_code == 402


def test_garbage_key_is_rejected():
    c = _app()
    assert c.get("/relay/v1/ws1/bundles", headers=_auth("ENGR1.!!!.???")).status_code == 402


def test_scoped_user_token_can_pull_but_needs_write_scope_to_push(monkeypatch, tmp_path):
    """Customer relays authorize the user token's scopes without exposing the Team key."""
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    active_license = licensing.parse_key(_key(plan="team"))
    monkeypatch.setattr(licensing, "current_license", lambda *a, **k: active_license)

    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    user = store.create_user(
        "member@example.com", "Member", "correct-horse-1", "member", seat_limit=1)
    read_token = store.create_api_token(
        user["id"], scopes=["agent", "sync:read"], ttl=600)["token"]
    write_token = store.create_api_token(
        user["id"], scopes=["agent", "sync:read", "sync:write"], ttl=600)["token"]

    workspace_store = Store(str(tmp_path / "memories.db"))
    workspace_store.create_workspace("ws1", settings={"visibility": "shared"})
    c = _app(service=SimpleNamespace(store=workspace_store))
    c.app.state.auth_store = store
    assert c.get("/relay/v1/ws1/names", headers=_auth(read_token)).status_code == 200
    denied = c.post(
        "/relay/v1/ws1/bundles/read-only.json", content=b"{}",
        headers=_auth(read_token))
    assert denied.status_code == 403 and "sync:write" in denied.text
    allowed = c.post(
        "/relay/v1/ws1/bundles/writer.json", content=b"{}",
        headers=_auth(write_token))
    assert allowed.status_code == 200

    store.update_user(user["id"], role="viewer")
    downgraded = c.post(
        "/relay/v1/ws1/bundles/no-longer-writer.json", content=b"{}",
        headers=_auth(write_token))
    assert downgraded.status_code == 403 and "read-only" in downgraded.text
    assert c.get("/relay/v1/ws1/names", headers=_auth(write_token)).status_code == 200


def test_scoped_user_token_cannot_address_personal_or_unknown_workspace(
        monkeypatch, tmp_path):
    """A Team token cannot turn the account relay into a personal-folder side channel."""
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    active_license = licensing.parse_key(_key(plan="team"))
    monkeypatch.setattr(licensing, "current_license", lambda *a, **k: active_license)

    auth_store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    user = auth_store.create_user(
        "member@example.com", "Member", "correct-horse-1", "member", seat_limit=1)
    token = auth_store.create_api_token(
        user["id"], scopes=["agent", "sync:read", "sync:write"], ttl=600)["token"]

    workspace_store = Store(str(tmp_path / "memories.db"))
    workspace_store.create_workspace("team-shared", settings={"visibility": "shared"})
    workspace_store.create_workspace(
        "member-private",
        settings={"visibility": "personal", "owner": "member@example.com"},
    )
    workspace_store.create_workspace(
        "invalid-visibility", settings={"visibility": "unexpected"})
    c = _app(service=SimpleNamespace(store=workspace_store))
    c.app.state.auth_store = auth_store
    headers = _auth(token)

    # Seed a legacy private bundle directly to model data uploaded by an older client.
    # The HTTP boundary must make it unreachable even to the folder's owner because this
    # database is account-wide rather than partitioned per user.
    account_id = reg.account_id_for(active_license)
    assert sync_relay._store_bundle(
        account_id, "member-private", "legacy-private.json", b"private",
    ) == (None, 200)

    assert c.post(
        "/relay/v1/team-shared/bundles/shared.json", content=b"{}", headers=headers,
    ).status_code == 200

    personal_paths = [
        ("GET", "/relay/v1/member-private/names"),
        ("GET", "/relay/v1/member-private/bundles"),
        ("GET", "/relay/v1/member-private/bundles/legacy-private.json"),
        ("POST", "/relay/v1/member-private/bundles/new.json"),
        ("DELETE", "/relay/v1/member-private/bundles/legacy-private.json"),
    ]
    for method, path in personal_paths:
        response = c.request(method, path, content=b"{}", headers=headers)
        assert response.status_code == 403
        assert "existing shared workspaces only" in response.text

    unknown = c.get("/relay/v1/guessed-personal-name/names", headers=headers)
    assert unknown.status_code == 403
    assert "existing shared workspaces only" in unknown.text
    malformed = c.get("/relay/v1/invalid-visibility/names", headers=headers)
    assert malformed.status_code == 403
    assert "existing shared workspaces only" in malformed.text


def test_expired_prefixed_user_token_never_falls_back_to_license_verification(
        monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    user = store.create_user(
        "member@example.com", "Member", "correct-horse-1", "member", seat_limit=1)
    issued = store.create_api_token(user["id"], scopes=["sync:read"], ttl=600)
    store.conn.execute("UPDATE api_tokens SET expires_at=0 WHERE id=?", (issued["id"],))
    store.conn.commit()

    c = _app()
    c.app.state.auth_store = store
    response = c.get("/relay/v1/ws1/names", headers=_auth(issued["token"]))
    assert response.status_code == 401
    assert "expired, revoked, or invalid" in response.json()["detail"]["error"]


# ── unauthenticated crypto is rate limited ────────────────────────────────────────────
# Every relay call runs a ~3ms pure-Python Ed25519 verify BEFORE anything authenticates
# the caller, and several handlers are sync defs that also pin a threadpool worker while
# they do it. license_cloud._relay_rate_ok bounds how much of that work an invalid-key
# flood from one IP can buy — using the relay's OWN budget, sized for a full sync round,
# NOT the /register bucket (a 60/min register budget would 429 the tail of every large
# round, and a 429 aborts the whole pull, so the round would never converge).

def test_invalid_key_flood_is_rate_limited_before_the_verify(monkeypatch):
    monkeypatch.setattr(license_cloud, "RELAY_RATE_PER_MINUTE", 3)
    license_cloud._RELAY_BUCKETS.clear()
    c = _app()
    bad = _auth("ENGR1.forged.forged")
    codes = [c.get("/relay/v1/ws1/names", headers=bad).status_code for _ in range(6)]
    assert codes[:3] == [402, 402, 402]           # budget spent on real verifies
    assert codes[3:] == [429, 429, 429]           # then refused before any crypto
    # A valid key is throttled by the same bucket — the limit is on the work, not on
    # being wrong, so a flood cannot be laundered through signature-valid keys.
    assert c.get("/relay/v1/ws1/names", headers=_auth(_key())).status_code == 429


def test_a_full_sync_round_is_not_throttled_by_the_relay_budget(monkeypatch):
    """Regression: the anti-DoS budget must not DoS legitimate sync. A full round is
    ~1 names + MAX_BUNDLES_PER_WORKSPACE bundle GETs + 1 push; at the default budget that
    whole round must go through, or large-workspace sync never converges (429 is fatal to
    the pull)."""
    monkeypatch.setattr(license_cloud, "RELAY_RATE_PER_MINUTE", 600)  # the shipped default
    license_cloud._RELAY_BUCKETS.clear()
    c = _app()
    key = _key()
    # Seed the workspace with the maximum number of bundles a round would pull.
    n = sync_relay.MAX_BUNDLES_PER_WORKSPACE
    for i in range(n):
        assert c.post("/relay/v1/ws1/bundles/bundle-%03d.json" % i,
                      content=b"{}", headers=_auth(key)).status_code == 200
    # A round: list names, GET every bundle, push this device's own bundle back (in place,
    # as a real round does) — none of these ~n+2 requests may be throttled.
    assert c.get("/relay/v1/ws1/names", headers=_auth(key)).status_code == 200
    for i in range(n):
        assert c.get("/relay/v1/ws1/bundles/bundle-%03d.json" % i,
                     headers=_auth(key)).status_code == 200
    assert c.post("/relay/v1/ws1/bundles/bundle-000.json",
                  content=b"{}", headers=_auth(key)).status_code == 200


def test_relay_and_register_budgets_are_independent(monkeypatch):
    """The relay must not drain the register budget or vice versa — they are separate
    surfaces with very different legitimate request rates."""
    monkeypatch.setattr(license_cloud, "RELAY_RATE_PER_MINUTE", 1)
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 10_000)
    license_cloud._RELAY_BUCKETS.clear()
    license_cloud._REGISTER_BUCKETS.clear()
    c = _app()
    # Spend the relay's single token, then confirm it is refused...
    assert c.get("/relay/v1/ws1/names", headers=_auth(_key())).status_code == 200
    assert c.get("/relay/v1/ws1/names", headers=_auth(_key())).status_code == 429
    # ...while the register budget (a different bucket) is untouched and still generous.
    for _ in range(5):
        assert license_cloud._register_rate_ok("testclient") is True


def test_relay_stays_up_when_the_limiter_itself_fails(monkeypatch):
    """Fail OPEN. A broken guard must not become a cheaper outage than the flood it
    guards against."""
    def boom(ip):
        raise RuntimeError("bucket storage unavailable")

    monkeypatch.setattr(license_cloud, "_relay_rate_ok", boom)
    c = _app()
    assert c.get("/relay/v1/ws1/names", headers=_auth(_key())).status_code == 200


def test_expired_key_is_rejected():
    c = _app()
    r = c.get("/relay/v1/ws1/bundles", headers=_auth(_key(expires_in_days=-1)))
    assert r.status_code == 402


def test_revoked_key_is_rejected():
    c = _app()
    key = _key()
    reg.record_issued(key)                       # in the registry, active
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(key)).status_code == 200
    lic = licensing.parse_key(key)
    assert reg.revoke(lic.key_id) is True
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(key)).status_code == 402


def test_accounts_are_isolated():
    c = _app()
    a, b = _key(email="a@x.com"), _key(email="b@x.com")
    c.post("/relay/v1/ws1/bundles/bundle-A.json", content=b"AAA", headers=_auth(a))
    # B shares the workspace name but a different license identity → sees nothing of A's
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(b)).json()["bundles"] == []
    c.post("/relay/v1/ws1/bundles/bundle-B.json", content=b"BBB", headers=_auth(b))
    a_view = c.get("/relay/v1/ws1/bundles", headers=_auth(a)).json()["bundles"]
    assert [x["name"] for x in a_view] == ["bundle-A.json"]  # still only A's own


def test_relay_rejects_invalid_names_and_streams_body_with_a_hard_cap(monkeypatch):
    c = _app()
    key = _key()
    assert c.post(
        "/relay/v1/ws1/bundles/.hidden.json", content=b"ok", headers=_auth(key)
    ).status_code == 400

    monkeypatch.setattr(sync_relay, "MAX_BUNDLE_BYTES", 4)
    oversized = c.post(
        "/relay/v1/ws1/bundles/bundle-a.json",
        content=b"12345",
        headers=_auth(key),
    )
    assert oversized.status_code == 413
    assert c.get(
        "/relay/v1/ws1/names", headers=_auth(key)
    ).json()["names"] == []


def test_relay_bounds_bundle_count_and_total_workspace_storage(monkeypatch):
    c = _app()
    key = _key()
    monkeypatch.setattr(sync_relay, "MAX_BUNDLES_PER_WORKSPACE", 1)
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACE_BYTES", 5)

    first = c.post(
        "/relay/v1/ws1/bundles/bundle-a.json", content=b"1234", headers=_auth(key)
    )
    assert first.status_code == 200
    assert c.post(
        "/relay/v1/ws1/bundles/bundle-b.json", content=b"1", headers=_auth(key)
    ).status_code == 413
    assert c.post(
        "/relay/v1/ws1/bundles/bundle-a.json", content=b"12345", headers=_auth(key)
    ).status_code == 200
    assert c.post(
        "/relay/v1/ws1/bundles/bundle-a.json", content=b"123456", headers=_auth(key)
    ).status_code == 413


def test_relay_enforces_account_byte_and_workspace_ceilings(monkeypatch):
    monkeypatch.setattr(sync_relay, "MAX_ACCOUNT_BYTES", 5)
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACES_PER_ACCOUNT", 2)
    account = "account"

    assert sync_relay._store_bundle(account, "ws1", "a.json", b"1234") == (None, 200)
    # A replacement subtracts the old row before projecting account usage.
    assert sync_relay._store_bundle(account, "ws1", "a.json", b"12") == (None, 200)
    assert sync_relay._store_bundle(account, "ws2", "b.json", b"123") == (None, 200)
    error, status = sync_relay._store_bundle(account, "ws3", "c.json", b"1")
    assert status == 413 and "workspaces" in error
    error, status = sync_relay._store_bundle(account, "ws2", "b.json", b"1234")
    assert status == 413 and "storage" in error


def test_zero_byte_bundle_is_still_detected_as_a_replacement(monkeypatch):
    monkeypatch.setattr(sync_relay, "MAX_BUNDLES_PER_WORKSPACE", 1)
    account = "account"
    assert sync_relay._store_bundle(account, "ws", "empty.json", b"") == (None, 200)
    # LENGTH(data)==0 used to double as the existence sentinel, so this replacement was
    # mistaken for a second bundle and rejected at the count ceiling.
    assert sync_relay._store_bundle(account, "ws", "empty.json", b"") == (None, 200)


def test_relay_quota_check_is_atomic_under_concurrent_pushes(monkeypatch):
    monkeypatch.setattr(sync_relay, "MAX_BUNDLES_PER_WORKSPACE", 1)
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACE_BYTES", 10)

    def write(index):
        return sync_relay._store_bundle(
            "account", "workspace", f"bundle-{index}.json", b"1"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(8)))

    assert sum(error is None for error, _status in results) == 1
    conn = sync_relay._conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM sync_bundles "
            "WHERE account_id='account' AND workspace_id='workspace'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_legacy_bulk_pull_is_bounded_but_raw_pull_still_works(monkeypatch):
    c = _app()
    key = _key()
    monkeypatch.setattr(sync_relay, "MAX_LEGACY_PULL_BYTES", 3)
    assert c.post(
        "/relay/v1/ws1/bundles/bundle-a.json", content=b"1234", headers=_auth(key)
    ).status_code == 200
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(key)).status_code == 413
    raw = c.get(
        "/relay/v1/ws1/bundles/bundle-a.json", headers=_auth(key)
    )
    assert raw.status_code == 200 and raw.content == b"1234"


def test_wrong_plan_feature_is_rejected():
    # a pro key does not grant 'team'; verify_for_feature must refuse it
    with pytest.raises(LicenseError, match="team"):
        reg.verify_for_feature(_key(plan="pro"), "team")


# ── registry unit behavior ─────────────────────────────────────────────────────────────

def test_registry_record_then_revoke():
    key = _key()
    kid = reg.record_issued(key)
    assert reg.is_revoked(kid) is False
    assert reg.revoke(kid) is True
    assert reg.is_revoked(kid) is True
    assert reg.revoke(kid) is False            # already revoked → no-op


def test_unknown_key_is_not_treated_as_revoked():
    # a validly-signed key with no registry row (sold pre-registry) is allowed
    assert reg.is_revoked("deadbeef0000") is False


def test_registry_unknown_revocation_survives_late_issuance_record():
    key = _key(subscription_id="sub-late-record")
    kid = licensing.parse_key(key).key_id

    assert reg.revoke(kid) is True
    assert reg.record_issued(key) == kid
    assert reg.is_revoked(kid) is True

    conn = reg.connect()
    try:
        row = conn.execute(
            "SELECT email, subscription_id, status FROM issued_licenses WHERE key_id=?",
            (kid,),
        ).fetchone()
    finally:
        conn.close()
    assert dict(row) == {
        "email": "buyer@example.com",
        "subscription_id": "sub-late-record",
        "status": "revoked",
    }


def test_registry_revokes_superseded_only_after_replacement_is_recorded():
    subscription_id = "sub-renewal"
    older = [
        _key(email="old-one@example.com", subscription_id=subscription_id),
        _key(email="old-two@example.com", subscription_id=subscription_id),
    ]
    replacement = _key(email="replacement@example.com", subscription_id=subscription_id)
    old_ids = [reg.record_issued(key) for key in older]
    replacement_id = licensing.parse_key(replacement).key_id

    assert reg.revoke_superseded(subscription_id, replacement_id) == 0
    assert not any(reg.is_revoked(kid) for kid in old_ids)

    assert reg.record_issued(replacement) == replacement_id
    assert reg.revoke_superseded(subscription_id, replacement_id) == 2
    assert all(reg.is_revoked(kid) for kid in old_ids)
    assert reg.is_revoked(replacement_id) is False


# ── client RelayTransport, end-to-end against the real endpoints ────────────────────────

def _wire_transport_to(client, monkeypatch):
    """Route the transport's urllib calls into the in-process TestClient."""
    class _Resp:
        def __init__(self, data): self._d = data
        def read(self, _limit=-1): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        headers = dict(req.headers)
        if req.method == "POST":
            resp = client.post(path, content=req.data or b"", headers=headers)
        else:
            resp = client.get(path, headers=headers)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.text,
                                         None, io.BytesIO(resp.content))
        return _Resp(resp.content)

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", fake_urlopen)


def test_relay_transport_roundtrip(monkeypatch):
    c = _app()
    _wire_transport_to(c, monkeypatch)
    t = RelayTransport("http://127.0.0.1", "ws1", license_key=_key())
    t.push("bundle-devA.json", b'{"m":1}')
    assert t.list_names() == ["bundle-devA.json"]
    assert list(t.pull()) == [("bundle-devA.json", b'{"m":1}')]


def test_relay_transport_surfaces_license_rejection(monkeypatch):
    c = _app()
    _wire_transport_to(c, monkeypatch)
    t = RelayTransport("http://127.0.0.1", "ws1", license_key="")  # no license
    with pytest.raises(RelayError) as ei:
        list(t.pull())
    assert ei.value.status == 402


def test_relay_transport_requires_https_off_loopback():
    with pytest.raises(ValueError, match="must use HTTPS"):
        RelayTransport("http://relay.example", "ws1", license_key="secret")
    with pytest.raises(ValueError, match="embedded credentials"):
        RelayTransport("https://user:pass@relay.example", "ws1", license_key="secret")
    with pytest.raises(ValueError, match="invalid port"):
        RelayTransport("https://relay.example:bad", "ws1", license_key="secret")
    with pytest.raises(ValueError, match="invalid host"):
        RelayTransport("https://relay host.example", "ws1", license_key="secret")
    with pytest.raises(ValueError, match="workspace_id"):
        RelayTransport("https://relay.example", "nested/workspace", license_key="secret")
    with pytest.raises(ValueError, match="bearer token"):
        RelayTransport("https://relay.example", "ws1", license_key="bad\nkey")


def test_relay_transport_rejects_invalid_name_response(monkeypatch):
    class _Response:
        def read(self, _limit):
            return b'{"names":["../escape.json"]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect",
                        lambda *args, **kwargs: _Response())
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    with pytest.raises(RelayError, match="invalid name response"):
        list(transport.pull())


def test_relay_transport_rejects_oversized_upload_before_network(monkeypatch):
    monkeypatch.setattr(relay_backend, "MAX_RELAY_BUNDLE_BYTES", 3)
    monkeypatch.setattr(
        relay_backend,
        "_urlopen_no_redirect",
        lambda *args, **kwargs: pytest.fail("oversized upload reached the network"),
    )
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    with pytest.raises(RelayError, match="upload safety limit"):
        transport.push("bundle-x.json", b"1234")


def test_relay_transport_rejects_invalid_upload_name_before_network(monkeypatch):
    monkeypatch.setattr(
        relay_backend,
        "_urlopen_no_redirect",
        lambda *args, **kwargs: pytest.fail("invalid name reached the network"),
    )
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    with pytest.raises(RelayError, match="name is invalid"):
        transport.push("../escape.json", b"{}")


def test_relay_transport_bounds_each_raw_bundle_response(monkeypatch):
    monkeypatch.setattr(relay_backend, "MAX_RELAY_BUNDLE_BYTES", 3)

    class _Response:
        def __init__(self, data):
            self.data = data

        def read(self, limit):
            return self.data[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/names"):
            return _Response(b'{"names":["bundle-a.json"]}')
        return _Response(b"1234")

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", fake_urlopen)
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    with pytest.raises(RelayError, match="safety limit"):
        list(transport.pull())


def test_relay_transport_has_bounded_fallback_for_first_generation_server(monkeypatch):
    class _Response:
        def __init__(self, data):
            self.data = data

        def read(self, limit=-1):
            return self.data if limit < 0 else self.data[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        if path.endswith("/names"):
            return _Response(b'{"names":["bundle-a.json"]}')
        if path.endswith("/bundles/bundle-a.json"):
            raise urllib.error.HTTPError(
                req.full_url, 404, "not found", None, io.BytesIO(b"")
            )
        assert path.endswith("/bundles")
        return _Response(b'{"bundles":[{"name":"bundle-a.json","data":"e30="}]}')

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", fake_urlopen)
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    assert list(transport.pull()) == [("bundle-a.json", b"{}")]


# ── per-bundle isolation in pull(): one bad bundle must not stall the round ─────────────

def _stub_relay(monkeypatch, names, bodies):
    """Serve ``names`` from /names and each bundle GET from ``bodies``.

    A ``bodies`` value is either the raw body bytes or an exception to raise for that
    bundle. Pushes succeed silently.
    """
    class _Response:
        def __init__(self, data):
            self.data = data

        def read(self, limit=-1):
            return self.data if limit < 0 else self.data[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        if req.method == "POST":
            return _Response(b"")
        if path.endswith("/names"):
            return _Response(json.dumps({"names": names}).encode("utf-8"))
        body = bodies[path.rsplit("/", 1)[-1]]
        if isinstance(body, BaseException):
            raise body
        return _Response(body)

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", fake_urlopen)


def _http_error(status):
    return urllib.error.HTTPError("https://relay.example/x", status, "nope", None,
                                  io.BytesIO(b"nope"))


def test_relay_pull_isolates_a_bad_bundle_and_still_delivers_the_rest(monkeypatch):
    """One undeliverable bundle must not starve the peers queued behind it.

    ``sync()`` already records a raising transport per bundle, but a generator that
    raises is closed and cannot be resumed — so the isolation has to happen here or the
    rest of the round is lost anyway."""
    _stub_relay(
        monkeypatch,
        ["bundle-a.json", "bundle-bad.json", "bundle-c.json"],
        {"bundle-a.json": b'{"a":1}',
         "bundle-bad.json": _http_error(500),
         "bundle-c.json": b'{"c":1}'},
    )
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    bundles = transport.pull()

    assert next(bundles) == ("bundle-a.json", b'{"a":1}')
    assert next(bundles) == ("bundle-c.json", b'{"c":1}')   # the point of the fix
    # …and the drop is still surfaced, after the good bundles, so the round can never
    # read as a success (sync() turns this into complete=False).
    with pytest.raises(RelayError, match="bundle-bad.json"):
        next(bundles)


@pytest.mark.parametrize("status", sorted(relay_backend.FATAL_PULL_STATUSES))
def test_relay_pull_never_isolates_a_round_level_refusal(monkeypatch, status):
    """Fail-closed: an unusable license, an authorization refusal or backpressure applies
    to every bundle in the round, so it aborts immediately instead of being retried per
    bundle — and the status stays visible to the caller."""
    _stub_relay(monkeypatch, ["bundle-a.json", "bundle-b.json"],
                {"bundle-a.json": b"{}", "bundle-b.json": _http_error(status)})
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    bundles = transport.pull()

    assert next(bundles) == ("bundle-a.json", b"{}")
    with pytest.raises(RelayError) as exc:
        next(bundles)
    assert exc.value.status == status


def test_relay_pull_aborts_the_round_when_the_relay_becomes_unreachable(monkeypatch):
    """A host that cannot be reached for one bundle cannot be reached for the next 63
    either; isolating that would burn a full timeout per name."""
    _stub_relay(monkeypatch, ["bundle-a.json", "bundle-b.json", "bundle-c.json"],
                {"bundle-a.json": b"{}",
                 "bundle-b.json": urllib.error.URLError("connection refused")})
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")
    bundles = transport.pull()

    assert next(bundles) == ("bundle-a.json", b"{}")
    with pytest.raises(RelayUnreachable):
        next(bundles)


def test_relay_pull_gives_up_after_too_many_isolated_failures(monkeypatch):
    monkeypatch.setattr(relay_backend, "MAX_PULL_BUNDLE_FAILURES", 2)
    names = ["bundle-%d.json" % i for i in range(5)]
    _stub_relay(monkeypatch, names, {name: _http_error(500) for name in names})
    transport = RelayTransport("https://relay.example", "ws1", license_key="secret")

    with pytest.raises(RelayError, match="skipped 2 bundle"):
        list(transport.pull())


def _peer_bundle(device, mem_id):
    return json.dumps({
        "format": SYNC_FORMAT, "version": 1, "device_id": device,
        "workspace_name": "w", "repos": {},
        "memories": [{"id": mem_id, "content": "from %s" % device, "last_access": 5.0}],
        "mem_links": [],
    }).encode("utf-8")


def test_sync_round_over_the_relay_applies_every_peer_behind_a_broken_bundle(monkeypatch):
    """End-to-end contract with core/sync.py: the peer *after* the poisoned bundle still
    lands, and the round is still reported as incomplete because a bundle was dropped."""
    _stub_relay(
        monkeypatch,
        ["bundle-peer1.json", "bundle-bad.json", "bundle-peer2.json"],
        {"bundle-peer1.json": _peer_bundle("dev_peer1", "mem_p1"),
         "bundle-bad.json": _http_error(500),
         "bundle-peer2.json": _peer_bundle("dev_peer2", "mem_p2")},
    )
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    transport = RelayTransport("https://relay.example", "w", license_key="secret")

    result = SyncEngine(store, allowed_workspaces=frozenset()).sync(
        transport, wid)      # must NOT raise

    assert store.get_memory("mem_p1") is not None
    assert store.get_memory("mem_p2") is not None        # was skipped before the fix
    assert result["totals"]["added"] == 2
    assert result["peers_applied"] == 2
    assert result["complete"] is False                   # a bundle was dropped
    assert result["errors"] == [{
        "bundle": "?", "error": "transport failure", "error_type": "RelayError"
    }]


def test_relay_transport_does_not_forward_bearer_across_redirects():
    redirected = threading.Event()

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            redirected.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"names":[]}')

        def log_message(self, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location",
                "http://127.0.0.1:%d/steal" % destination.server_address[1],
            )
            self.end_headers()

        def log_message(self, *_args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    redirector_thread = threading.Thread(target=redirector.serve_forever, daemon=True)
    redirector_thread.start()
    try:
        transport = RelayTransport(
            "http://127.0.0.1:%d" % redirector.server_address[1],
            "ws1",
            license_key="must-not-leak",
        )
        with pytest.raises(RelayError) as exc:
            transport.list_names()
        assert exc.value.status == 302
        assert redirected.is_set() is False
    finally:
        redirector.shutdown()
        destination.shutdown()
        redirector.server_close()
        destination.server_close()
        redirector_thread.join(timeout=2)
        destination_thread.join(timeout=2)


# ── Team seat enforcement at the relay: non-shareable beyond `seats`, seats float ───────

def _tkey(seats=2, email="team@corp.com"):
    now = time.time()
    return licensing.compose_key(
        {"v": 1, "plan": "team", "email": email, "seats": seats,
         "issued": int(now), "expires": int(now + 30 * 86400)}, SECRET)


def _dev(key, mid):
    return {"Authorization": "Bearer %s" % key, "X-Engraphis-Machine-Id": mid}


def test_team_relay_requires_machine_id():
    c = _app()
    r = c.get("/relay/v1/ws1/bundles", headers=_auth(_tkey()))   # no device id header
    assert r.status_code == 402 and "device id" in r.json()["error"].lower()


def test_team_relay_rejects_unbounded_machine_id():
    c = _app()
    headers = _auth(_tkey())
    headers["X-Engraphis-Machine-Id"] = "x" * 201
    response = c.get("/relay/v1/ws1/names", headers=headers)
    assert response.status_code == 402
    assert "device id" in response.json()["error"].lower()


def test_team_relay_enforces_seat_cap():
    c = _app()
    k = _tkey(seats=2)
    for m in ("d1", "d2"):
        assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, m)).status_code == 200
    over = c.get("/relay/v1/ws1/bundles", headers=_dev(k, "d3"))
    assert over.status_code == 402 and "seat" in over.json()["error"].lower()
    # an already-seated device keeps working (refresh, not a new claim)
    assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, "d1")).status_code == 200
    # pushing is gated too, not just pulling
    assert c.post("/relay/v1/ws1/bundles/x.json", content=b"{}",
                  headers=_dev(k, "d3")).status_code == 402


def test_team_relay_reclaims_idle_seat(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LEASE_TTL_HOURS", "1")        # small reclaim window
    c = _app()
    k = _tkey(seats=1)
    kid = licensing.parse_key(k).key_id
    assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, "d1")).status_code == 200
    assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, "d2")).status_code == 402
    conn = reg.connect()                                        # age d1 past the window
    conn.execute("UPDATE registrations SET last_seen=? WHERE key_id=? AND machine_id=?",
                 (time.time() - 10 * 3600, kid, "d1"))
    conn.commit()
    conn.close()
    assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, "d2")).status_code == 200


def test_pro_relay_is_not_device_capped():
    # Pro is the individual multi-device tier (seats=1): many of ONE person's devices sync
    # under the same account, so the relay must NOT seat-cap Pro even with a device header.
    c = _app()
    k = _key(plan="pro")                                        # seats=1
    for m in ("p1", "p2", "p3"):
        assert c.get("/relay/v1/ws1/bundles", headers=_dev(k, m)).status_code == 200
