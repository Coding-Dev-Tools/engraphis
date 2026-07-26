"""Launch guards for *which plan* the client says a customer is on.

``/api/license`` learned to report a real feature list, which fixed the free-vs-paid lock
badges. It still guessed the plan: ``_hosted_plan`` read an undocumented
``ENGRAPHIS_CLOUD_PLAN`` and otherwise returned ``"pro"`` for any connected installation.
A paying TEAM customer who had never set that variable was therefore shown a PRO badge and
a lock on the Team administration they were paying for.

The control plane does know. It just never told this client: ``POST /v1/devices/register``
and ``POST /v1/tokens/refresh`` both answer with ``DeviceRegistrationResponse``, whose only
entitlement field is ``entitlement_version`` — so ``cloud_session.save_bootstrap`` has
nothing to persist. ``GET /v1/entitlements/{organization_id}`` *is* authoritative, and
every access token this client can mint already carries the ``entitlement:read`` scope it
requires. The client now reads it opportunistically and caches the answer.

Every test here pins one half of that bargain:

* the badge and the feature list must follow the control plane's own answer; and
* getting that answer must never block boot, hang, or raise — the exact failure modes
  fixed earlier in this release cycle, on a path that now makes a network call.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import socket
import threading
import time
import typing
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ``engraphis.routes.v2_api`` imports FastAPI, which the numpy-only core floor job does not
# install. Skip rather than error at collection, matching the rest of the suite.
pytest.importorskip("fastapi", reason="full-stack extra not installed")

from engraphis import cloud_session  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = REPO_ROOT / "engraphis" / "static" / "dashboard.js"

ORGANIZATION = "org_paying_team"
CONTROL_URL = "https://control.example.test"

# The authoritative payload shape, mirrored from (read-only)
# engraphis-cloud/engraphis_cloud/entitlements.py ``EntitlementDTO`` / ``PLAN_FEATURES``.
SERVER_PLAN_FEATURES = {
    "free": [],
    "pro": ["analytics", "automation", "export", "sync"],
    "team": ["analytics", "automation", "export", "sync", "team"],
}


def _entitlement_dto(plan: str, *, active: bool = True,
                     organization_id: str = ORGANIZATION) -> dict:
    return {
        "organization_id": organization_id,
        "entitlement_id": "ent_1",
        "plan": plan,
        "status": "active" if active else "past_due",
        "cloud_access_active": active,
        "cloud_features": SERVER_PLAN_FEATURES[plan] if active else [],
        "seat_limit": 5,
        "seat_assignment_basis": "named_members",
        "starts_at": "2026-01-01T00:00:00Z",
        "expires_at": None,
        "version": 3,
    }


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class _FakeControlPlane:
    """One scripted control plane, recording exactly what the client sent it."""

    def __init__(self, entitlement=None, *, error=None, raw=None, delay=None,
                 registration=None):
        self.entitlement = entitlement
        self.error = error
        self.raw = raw
        self.delay = delay
        #: The entitlement fields a *current* control plane puts on every
        #: ``DeviceRegistrationResponse``. ``None`` models a deployment that predates them.
        self.registration = registration
        self.requests = []
        self.threads = []

    def opener(self, *handlers):
        self.handlers = handlers
        return self

    def open(self, request, timeout=None):
        self.threads.append(threading.current_thread())
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "timeout": timeout,
            "authorization": request.get_header("Authorization"),
        })
        if request.full_url.endswith("/v1/tokens/refresh"):
            body = {
                "access_token": "eyJ-scoped-access",
                "organization_id": ORGANIZATION,
                "refresh_credential": "engr_rt_rotated_%d" % len(self.requests),
                "refresh_expires_at": "2026-08-01T00:00:00Z",
                "token_subject": "member",
            }
            body.update(self.registration or {})
            return _Response(json.dumps(body).encode("utf-8"))
        if self.delay is not None:
            self.delay.wait(timeout=10.0)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return _Response(self.raw)
        return _Response(json.dumps(self.entitlement).encode("utf-8"))


@pytest.fixture(autouse=True)
def _plan_resolution_isolation(monkeypatch):
    """Keep every test hermetic: no inherited override, no leaked in-flight refresh."""

    monkeypatch.delenv("ENGRAPHIS_CLOUD_PLAN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_CONTROL_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", raising=False)
    monkeypatch.setattr(v2_api, "_entitlement_refreshing", False, raising=False)
    yield
    _drain_refresh()
    v2_api._entitlement_refreshing = False


def _connect(monkeypatch, *, pinned_token: bool = True) -> None:
    """Present this process as an onboarded installation.

    ``pinned_token=True`` uses the documented short-lived-access-token deployment, which
    reaches the entitlement route in a single request. ``False`` exercises the ordinary
    saved-refresh-credential path, which rotates a credential first.
    """

    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", CONTROL_URL)
    if pinned_token:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "eyJ-scoped-access")
        monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", ORGANIZATION)
    else:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "engr_rt_bootstrap")


def _serve(monkeypatch, cloud: _FakeControlPlane) -> None:
    monkeypatch.setattr(
        "engraphis.hosted_client.build_pinned_https_opener", cloud.opener
    )
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener", cloud.opener)


def _drain_refresh(timeout: float = 10.0) -> None:
    """Block until the background refresh finishes, so assertions are deterministic."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with v2_api._ENTITLEMENT_REFRESH_LOCK:
            busy = v2_api._entitlement_refreshing
        if not busy:
            return
        time.sleep(0.005)
    raise AssertionError("the background entitlement refresh never finished")


def _entitlement_reads(cloud: _FakeControlPlane) -> list:
    return [call for call in cloud.requests if "/v1/entitlements/" in call["url"]]


def _settled_license(monkeypatch) -> dict:
    """Read ``/api/license`` once to schedule the refresh, then again once it lands."""

    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)
    v2_api.get_license()
    _drain_refresh()
    # Stop scheduling further refreshes, so the recorded traffic is exactly one round and
    # assertions about it are not racing a second background thread.
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 3600)
    return v2_api.get_license()


# ── (1) a Team customer must be shown Team, with Team unlocked ────────────────
def test_a_team_entitlement_produces_a_team_badge_with_the_team_feature_unlocked(
    monkeypatch,
) -> None:
    """The defect: a paying TEAM customer saw ``PRO`` and a lock on Team administration."""

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "cloud"
    assert "team" in payload["features"], "Team is still locked for a Team customer"
    # The server folds these into ``automation``; the panel lists them separately, so a
    # verbatim echo would leave a paying customer looking at unticked rows.
    assert {"consolidation", "dreaming"} <= set(payload["features"])
    assert set(SERVER_PLAN_FEATURES["team"]) <= set(payload["features"])
    assert payload["cloud_access_active"] is True
    assert payload["plan_checked_at"] > 0


def test_a_team_entitlement_arrives_over_the_ordinary_refresh_credential_path(
    monkeypatch,
) -> None:
    """Not just the pinned-token deployment: the normal saved-session path works too."""

    _connect(monkeypatch, pinned_token=False)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert "team" in payload["features"]
    assert cloud.requests[0]["url"] == CONTROL_URL + "/v1/tokens/refresh"
    reads = _entitlement_reads(cloud)
    assert [call["url"] for call in reads] == [
        CONTROL_URL + "/v1/entitlements/" + ORGANIZATION
    ]


def test_a_pro_entitlement_keeps_the_team_upsell_visible(monkeypatch) -> None:
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("pro")))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "pro"
    assert {"analytics", "automation"} <= set(payload["features"])
    assert "team" not in payload["features"]


def test_a_connected_free_organization_is_reported_as_the_local_core(monkeypatch) -> None:
    """Being connected is not the same as having paid; the plan now says which."""

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("free", active=False)))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "local"
    assert payload["features"] == []


def test_a_lapsed_subscription_keeps_its_plan_name_but_loses_its_features(
    monkeypatch,
) -> None:
    """A past-due Team customer needs the billing portal, not another trial offer.

    The dashboard offers "Open Team Cloud" for a hosted plan and "Start hosted Team trial"
    otherwise, so collapsing a lapsed Team customer to ``local`` would send them to a trial
    they have already consumed instead of to the page that restores their access.
    """

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team", active=False)))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert payload["features"] == []
    assert payload["cloud_access_active"] is False


def test_the_dashboards_gated_navigation_unlocks_for_an_authoritative_team_plan(
    monkeypatch,
) -> None:
    """Close the loop on the shipped JS: ``locked = !features.includes(f)``."""

    script = DASHBOARD_JS.read_text(encoding="utf-8")
    block = script[script.index("function updateFeatureLocks()"):]
    gated = re.findall(r"apply\('[^']+',\s*'([^']+)'", block[:block.index("\n}")])
    assert set(gated) == {"analytics", "automation", "team"}, gated

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    payload = _settled_license(monkeypatch)

    for feature in gated:
        assert feature in payload["features"], "Team still renders a lock on %s" % feature
    # Every advertised key must be renderable by the license panel.
    assert set(payload["features"]) <= set(payload["known_features"])


# ── (2) the boot path must not block, hang, or raise ──────────────────────────
def test_the_authoritative_read_never_runs_on_the_request_thread(monkeypatch) -> None:
    """``/api/license`` is on the ``/api/bootstrap`` boot path."""

    _connect(monkeypatch)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)

    _settled_license(monkeypatch)

    assert cloud.threads, "the control plane was never contacted"
    for thread in cloud.threads:
        assert thread is not threading.main_thread()
        assert thread.daemon, "a boot-path helper thread must never hold the process open"


def test_a_hanging_control_plane_does_not_delay_boot(monkeypatch) -> None:
    """The regression that matters most: a stalled cloud must not stall the dashboard."""

    _connect(monkeypatch)
    release = threading.Event()
    cloud = _FakeControlPlane(_entitlement_dto("team"), delay=release)
    _serve(monkeypatch, cloud)
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)

    try:
        started = time.monotonic()
        payload = v2_api.get_license()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, "the boot path waited %.2fs on the cloud" % elapsed
        # A connected installation is never shown the free local core while it waits.
        assert payload["plan"] == "pro"
        assert payload["plan_source"] == "connected"
        assert payload["features"]
    finally:
        release.set()


def test_the_entitlement_read_is_bounded_authenticated_and_redirect_proof(
    monkeypatch,
) -> None:
    """The request carries a live bearer, so it needs a budget and no redirect following."""

    _connect(monkeypatch)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)

    _settled_license(monkeypatch)

    entitlement = _entitlement_reads(cloud)[0]
    assert entitlement["method"] == "GET"
    assert entitlement["authorization"] == "Bearer eyJ-scoped-access"
    assert isinstance(entitlement["timeout"], (int, float))
    assert 0 < entitlement["timeout"] <= 15
    assert cloud.handlers, "the no-redirect handler must be installed"
    assert any(
        isinstance(handler, urllib.request.HTTPRedirectHandler)
        and type(handler).__name__ == "_NoRedirect"
        for handler in cloud.handlers
    )


def test_the_probe_uses_the_repos_vetted_connector() -> None:
    """A credential-bearing call must not build an unvetted opener."""

    source = (REPO_ROOT / "engraphis" / "routes" / "v2_api.py").read_text(encoding="utf-8")

    assert "build_pinned_https_opener" in source
    assert "urllib.request.build_opener(" not in source


@pytest.mark.parametrize("control_url,resolves_to", [
    # The pinned opener replaces urllib's *HTTPS* handler only, so a cleartext endpoint
    # would fall through to the plain handler and put a live bearer on the wire.
    ("http://control.example.test", "93.184.216.34"),
    ("https://user:secret@control.example.test", "93.184.216.34"),
    ("https://control.example.test/v1?leak=1", "93.184.216.34"),
    ("ftp://control.example.test", "93.184.216.34"),
    ("https://internal.example.test", "10.0.0.5"),
])
def test_an_unvetted_control_url_never_receives_the_bearer_token(
    monkeypatch, control_url, resolves_to,
) -> None:
    """A pinned access token short-circuits ``cloud_session``'s own URL validation.

    ``access_for_workspace`` validates the control URL on the saved-session path but
    returns immediately when ``ENGRAPHIS_CLOUD_ACCESS_TOKEN`` is configured, so this path
    has to vet the endpoint itself before it attaches a credential to it.
    """

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(
        socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolves_to, port or 0))])
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "eyJ-scoped-access")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", ORGANIZATION)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", control_url)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)
    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *a, **k: pytest.fail("an unvetted opener was built for " + control_url),
    )

    assert v2_api._fetch_authoritative_entitlement() is None
    assert cloud.requests == []


def test_a_vetted_https_control_url_is_still_reached(monkeypatch) -> None:
    """The guard above must reject bad endpoints without breaking the good one."""

    _connect(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", CONTROL_URL + "/")
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)

    assert _settled_license(monkeypatch)["plan"] == "team"
    assert [call["url"] for call in _entitlement_reads(cloud)] == [
        CONTROL_URL + "/v1/entitlements/" + ORGANIZATION
    ]


def test_the_entitlement_refresh_never_sends_a_null_workspace_id(monkeypatch) -> None:
    """The compatibility fallback mints an *unbound* token, so it passes no workspace.

    ``access_for_workspace`` is annotated ``workspace_id: str`` but is called with ``None``,
    which ``_post_refresh`` serialises as ``"workspace_id": null``. A control plane that
    requires a non-null workspace id answers 4xx, ``_fetch_authoritative_entitlement``
    swallows it and returns ``None``, and the cached ``GET /v1/entitlements/{org}`` answer
    that older control planes depend on is never written at all. Omit the key instead.
    """

    _connect(monkeypatch, pinned_token=False)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    sent = []
    record = cloud.open

    def _capture(request, timeout=None):
        sent.append(request.data)
        return record(request, timeout=timeout)

    monkeypatch.setattr(cloud, "open", _capture)
    _serve(monkeypatch, cloud)

    v2_api._fetch_authoritative_entitlement()

    payloads = [json.loads(body.decode("utf-8")) for body in sent if body]
    assert payloads, "the token refresh was never made"
    for payload in payloads:
        assert payload.get("workspace_id", "absent") is not None, payload
    hints = typing.get_type_hints(cloud_session.access_for_workspace)
    assert hints["workspace_id"] == typing.Optional[str]


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError(CONTROL_URL, 401, "unauthorized", {}, io.BytesIO(b"{}")),
    urllib.error.HTTPError(CONTROL_URL, 402, "payment required", {}, io.BytesIO(b"{}")),
    urllib.error.HTTPError(CONTROL_URL, 500, "server error", {}, io.BytesIO(b"{}")),
    urllib.error.URLError("offline"),
    TimeoutError("the read timed out"),
    OSError("connection reset"),
])
def test_a_cloud_failure_never_reaches_the_dashboard(monkeypatch, failure) -> None:
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(error=failure))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "pro"  # the connected fallback, not an exception
    assert payload["features"]
    assert v2_api._read_entitlement_cache() == {}


def test_an_unreadable_error_body_never_escapes_the_refresh_thread(monkeypatch) -> None:
    """Draining an ``HTTPError`` body can itself raise; a sibling ``except`` misses it."""

    class _Unreadable(io.BytesIO):
        def read(self, *args, **kwargs):
            raise TimeoutError("the read timed out")

        def close(self):
            if self.closed:
                return
            # Model a reset after the descriptor was released so this deliberate
            # cleanup failure does not recur from BytesIO's finalizer as a warning.
            super().close()
            raise OSError("the socket was already reset")

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(error=urllib.error.HTTPError(
        CONTROL_URL, 503, "unavailable", {}, _Unreadable())))

    assert _settled_license(monkeypatch)["plan"] == "pro"


@pytest.mark.parametrize("body", [
    b"{not json",
    b"[1, 2, 3]",
    b"null",
    # A truncated body must not coerce to "free, no access" and cache a paying customer
    # as the free local core: ``plan`` and ``cloud_access_active`` are required fields.
    b'{"organization_id": "org_paying_team"}',
    b'{"organization_id":"org_paying_team","plan":"team"}',
    b'{"organization_id":"org_paying_team","plan":"","cloud_access_active":true}',
    b'{"organization_id":"org_paying_team","plan":7,"cloud_access_active":true}',
    b'{"organization_id":"org_paying_team","plan":"team","cloud_access_active":"yes"}',
    json.dumps(_entitlement_dto("team", organization_id="org_someone_else")).encode(),
    b'{"organization_id":"org_paying_team","plan":"team","cloud_access_active":true,'
    + b'"pad":"' + b"x" * 70_000 + b'"}',
], ids=[
    "invalid-json",
    "array",
    "null",
    "incomplete",
    "missing-activity",
    "empty-plan",
    "numeric-plan",
    "nonboolean-activity",
    "wrong-organization",
    "oversized",
])
def test_a_malformed_or_misrouted_answer_never_relabels_the_plan(monkeypatch, body) -> None:
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(raw=body))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "pro"
    assert v2_api._read_entitlement_cache() == {}


# ── (3) offline: the cache is what keeps a paying customer correct ────────────
def test_an_offline_client_still_boots_and_renders_its_last_known_plan(
    monkeypatch,
) -> None:
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    assert _settled_license(monkeypatch)["plan"] == "team"

    # Every route to the cloud now fails, exactly as it does on a plane. The zero interval
    # guarantees a refresh is actually attempted (and fails) during the read below.
    _serve(monkeypatch, _FakeControlPlane(error=urllib.error.URLError("offline")))
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)
    started = time.monotonic()
    payload = v2_api.get_license()
    elapsed = time.monotonic() - started
    _drain_refresh()

    assert elapsed < 1.0
    assert payload["plan"] == "team"
    assert "team" in payload["features"]
    assert payload["plan_source"] == "cloud"
    # A failed refresh must not erase the last good answer.
    assert v2_api._read_entitlement_cache()["plan"] == "team"


def test_the_real_boot_route_serves_a_connected_offline_client(monkeypatch, tmp_path) -> None:
    """The whole point, over real HTTP: ``/api/bootstrap`` must render with no cloud."""

    pytest.importorskip("fastapi", reason="full-stack extra not installed")
    pytest.importorskip("httpx", reason="httpx not installed")
    from fastapi.testclient import TestClient

    from engraphis.config import settings

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    assert _settled_license(monkeypatch)["plan"] == "team"

    # Now cut the cloud off entirely, as an aeroplane or a captive portal would, and let
    # the boot path try (and fail) to refresh while it serves.
    _serve(monkeypatch, _FakeControlPlane(error=urllib.error.URLError("offline")))
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "boot.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(v2_api, "_service", None)

    from engraphis.dashboard_app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = time.monotonic()
        boot = client.get("/api/bootstrap")
        elapsed = time.monotonic() - started
        page = client.get("/")

    assert boot.status_code == 200
    assert page.status_code == 200
    assert elapsed < 5.0, "the boot route waited %.2fs on an unreachable cloud" % elapsed
    license_state = boot.json()["license"]
    assert license_state["plan"] == "team"
    assert "team" in license_state["features"]


def test_an_offline_unconnected_client_is_the_free_local_core(monkeypatch) -> None:
    _serve(monkeypatch, _FakeControlPlane(error=urllib.error.URLError("offline")))

    payload = v2_api.get_license()

    assert payload["plan"] == "local"
    assert payload["features"] == []
    assert payload["plan_source"] == "local"


def test_an_unconnected_installation_starts_no_background_work(monkeypatch) -> None:
    """No session, no thread, no state written — an offline gate must stay quiet."""

    before = threading.active_count()

    for _ in range(3):
        assert v2_api.get_license()["plan"] == "local"

    assert threading.active_count() <= before
    assert v2_api._entitlement_refreshing is False
    assert v2_api._read_entitlement_cache() == {}


def test_a_connected_installation_with_no_control_url_starts_no_background_work(
    monkeypatch,
) -> None:
    """Nothing to dial means nothing to schedule."""

    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "eyJ-scoped-access")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", ORGANIZATION)
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)

    before = threading.active_count()
    assert v2_api.get_license()["plan"] == "pro"

    assert threading.active_count() <= before
    assert v2_api._entitlement_refreshing is False


def test_a_corrupt_cache_is_discarded_rather_than_downgrading_a_paying_customer(
    monkeypatch,
) -> None:
    """Coercing a damaged value would quietly relabel a paying customer as free."""

    _connect(monkeypatch)
    path = v2_api._entitlement_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for blob in ("not json",
                 '{"schema": "someone-elses/v1", "plan": "team"}',
                 '{"schema": "engraphis-cloud-entitlement/v1", "plan": {"a": 1}}',
                 '{"schema": "engraphis-cloud-entitlement/v1", "plan": "enterprise"}'):
        path.write_text(blob, encoding="utf-8")

        assert v2_api._read_entitlement_cache() == {}
        assert v2_api._hosted_plan() == "pro"


def test_the_cache_is_written_owner_only_and_reread_across_processes(
    monkeypatch,
) -> None:
    """It lives beside the cloud session, so a partial or world-readable file is not ok."""

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    _settled_license(monkeypatch)

    path = v2_api._entitlement_cache_path()
    assert path.exists()
    # Windows does not project ACLs through POSIX mode bits.  The private-state helper
    # still uses its race-safe write/read path there, but only POSIX can prove owner-only
    # access with this portable ``st_mode`` check.
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema"] == "engraphis-cloud-entitlement/v1"
    assert saved["plan"] == "team"
    assert saved["organization_id"] == ORGANIZATION
    # No credential may be persisted alongside the presentation state.
    assert "eyJ-scoped-access" not in path.read_text(encoding="utf-8")


def test_an_unwritable_state_directory_costs_freshness_not_the_dashboard(
    monkeypatch,
) -> None:
    def _refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("engraphis.private_state.atomic_private_text", _refuse)
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "pro"
    assert payload["features"]


# ── (4) refresh scheduling ────────────────────────────────────────────────────
def test_only_one_refresh_is_ever_in_flight(monkeypatch) -> None:
    _connect(monkeypatch)
    release = threading.Event()
    cloud = _FakeControlPlane(_entitlement_dto("team"), delay=release)
    _serve(monkeypatch, cloud)
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)

    try:
        for _ in range(8):
            v2_api.get_license()
        deadline = time.monotonic() + 5.0
        while not cloud.requests and time.monotonic() < deadline:
            time.sleep(0.005)
        entitlement_reads = [
            call for call in cloud.requests if "/v1/entitlements/" in call["url"]
        ]
        assert len(entitlement_reads) == 1, entitlement_reads
    finally:
        release.set()


def test_a_fresh_cache_is_not_refetched(monkeypatch) -> None:
    _connect(monkeypatch)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)
    _settled_license(monkeypatch)
    reads = len([c for c in cloud.requests if "/v1/entitlements/" in c["url"]])

    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 3600)
    for _ in range(5):
        assert v2_api.get_license()["plan"] == "team"
    _drain_refresh()

    assert len([c for c in cloud.requests if "/v1/entitlements/" in c["url"]]) == reads


def test_the_refresh_has_an_operator_kill_switch(monkeypatch) -> None:
    _connect(monkeypatch)
    cloud = _FakeControlPlane(_entitlement_dto("team"))
    _serve(monkeypatch, cloud)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", "0")
    monkeypatch.setattr(v2_api, "_ENTITLEMENT_REFRESH_SECONDS", 0)

    for _ in range(3):
        assert v2_api.get_license()["plan"] == "pro"
    _drain_refresh()

    assert cloud.requests == []


def test_the_documented_kill_switch_and_override_are_in_the_example_config() -> None:
    """P3: a config key the client reads must not be discoverable only from the source."""

    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "ENGRAPHIS_CLOUD_PLAN=" in example
    assert "ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH=" in example
    # Both must be commented out: neither is required for a correct badge.
    assert "\nENGRAPHIS_CLOUD_PLAN=" not in example
    assert "\nENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH=" not in example
    # And ENGRAPHIS_CLOUD_PLAN must read as the escape hatch it now is, not as the way
    # plans are normally resolved — that framing is what made it undiscoverable.
    assert "ENGRAPHIS_CLOUD_PLAN is an override" in example
    assert "The plan is resolved automatically" in example


# ── (4b) the plan now rides registration and refresh ──────────────────────────
# ``DeviceRegistrationResponse`` — returned by both ``/internal/devices/register`` and
# ``POST /v1/tokens/refresh`` — carries ``plan``, ``cloud_features`` and
# ``cloud_access_active``. The client consumes them instead of polling a second route for
# a fact those calls already deliver, while staying compatible with a cloud that omits them.
def _registration_entitlement(plan: str, *, active: bool = True) -> dict:
    return {
        "plan": plan,
        "cloud_features": SERVER_PLAN_FEATURES[plan] if active else [],
        "cloud_access_active": active,
    }


def test_the_plan_arrives_on_the_refresh_the_client_already_makes(monkeypatch) -> None:
    """The point of the change: one call, not two, and the answer is authoritative."""

    _connect(monkeypatch, pinned_token=False)
    cloud = _FakeControlPlane(_entitlement_dto("team"),
                              registration=_registration_entitlement("team"))
    _serve(monkeypatch, cloud)

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "session"
    assert "team" in payload["features"]
    assert {"consolidation", "dreaming"} <= set(payload["features"])
    assert payload["cloud_access_active"] is True
    assert payload["plan_checked_at"] > 0
    # The dedicated entitlements poll is redundant against this control plane and must not
    # be made: the token refresh already answered.
    assert _entitlement_reads(cloud) == []
    assert [call["url"] for call in cloud.requests] == [
        CONTROL_URL + "/v1/tokens/refresh"
    ]


def test_registration_alone_makes_the_very_first_boot_correct(monkeypatch) -> None:
    """Onboarding persists the plan, so boot needs no network to render Team."""

    _connect(monkeypatch, pinned_token=False)
    cloud_session.save_bootstrap(
        dict({
            "refresh_credential": "engr_rt_bootstrap",
            "organization_id": ORGANIZATION,
            "installation_id": "inst_1",
            "device_id": "dev_1",
            "member_id": "mem_1",
            "token_subject": "member",
        }, **_registration_entitlement("team")),
        control_url=CONTROL_URL,
        compute_url=CONTROL_URL,
    )
    cloud = _FakeControlPlane(_entitlement_dto("pro"))
    _serve(monkeypatch, cloud)

    started = time.monotonic()
    payload = v2_api.get_license()
    elapsed = time.monotonic() - started
    _drain_refresh()

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "session"
    assert "team" in payload["features"]
    # A just-registered answer is fresh, so boot touches the network not at all.
    assert cloud.requests == []
    assert elapsed < 1.0


def test_a_control_plane_without_the_new_fields_still_resolves_the_plan(
    monkeypatch,
) -> None:
    """The client must not require a newer server: the old route is the fallback."""

    _connect(monkeypatch, pinned_token=False)
    cloud = _FakeControlPlane(_entitlement_dto("team"), registration=None)
    _serve(monkeypatch, cloud)

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "cloud"
    assert "team" in payload["features"]
    assert [call["url"] for call in _entitlement_reads(cloud)] == [
        CONTROL_URL + "/v1/entitlements/" + ORGANIZATION
    ]
    # Nothing was persisted onto the session, so the fallback stays in charge.
    assert cloud_session.saved_entitlement() == {}


def test_a_field_less_refresh_never_erases_a_plan_the_cloud_already_declared(
    monkeypatch,
) -> None:
    """A rollback of the server change must not downgrade a paying customer."""

    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert _settled_license(monkeypatch)["plan"] == "team"

    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"), registration=None))
    cloud_session.access_for_workspace(None, require_compute=False)

    assert cloud_session.saved_entitlement()["plan"] == "team"
    assert v2_api.get_license()["plan"] == "team"


def test_a_downgrade_drops_the_previous_plans_features_from_the_resolved_answer(
    monkeypatch,
) -> None:
    """Team -> Pro must lock the Team tab, not leave it open on a stale grant list.

    ``_declared_entitlement`` omits ``cloud_features`` when the refresh body carries no
    feature list, so merging it onto the saved record swapped ``plan`` while the *old* list
    stayed behind. ``/api/license`` then answered ``pro`` with ``team`` still in
    ``features``, and the customer kept the Team administration they had stopped paying for.
    """

    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert "team" in _settled_license(monkeypatch)["features"]

    # The downgrade as a control plane that names the plan but no features reports it.
    _serve(monkeypatch, _FakeControlPlane(
        _entitlement_dto("pro"),
        registration={"plan": "pro", "cloud_access_active": True},
    ))
    cloud_session.access_for_workspace(None, require_compute=False)

    saved = cloud_session.saved_entitlement()
    assert saved["plan"] == "pro"
    assert "team" not in saved.get("cloud_features", [])

    payload = v2_api.get_license()

    assert payload["plan"] == "pro"
    assert payload["plan_source"] == "session"
    assert "team" not in payload["features"], "Team is still unlocked on a Pro plan"
    # A stale list is dropped, not blanked: everything Pro does grant is still granted.
    assert set(SERVER_PLAN_FEATURES["pro"]) <= set(payload["features"])


def test_a_billing_denial_stops_the_session_claiming_paid_access(monkeypatch) -> None:
    """A 402 is an answer, not an outage.

    The saved session outranks the entitlement cache, so folding the control plane's 402
    in with offline/transport failures left ``cloud_access_active`` true and kept the
    dashboard advertising paid features indefinitely -- while every hosted call was
    denied. The plan name is kept so the UI can still say which plan lapsed.
    """

    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert _settled_license(monkeypatch)["cloud_access_active"] is True

    def _lapsed(*_args, **_kwargs):
        raise cloud_session.CloudSessionError("Subscription is not active.", status=402)

    monkeypatch.setattr(cloud_session, "access_for_workspace", _lapsed)
    assert v2_api._fetch_authoritative_entitlement() is None

    record = cloud_session._load()
    assert record.get("cloud_access_active") is False
    assert record.get("cloud_features") == []
    assert record.get("plan") == "team", "the lapsed plan is still named for the UI"

    payload = v2_api.get_license()
    assert payload["cloud_access_active"] is False
    assert payload["features"] == []


def test_a_transport_failure_is_not_mistaken_for_a_billing_denial(monkeypatch) -> None:
    """Only 402 clears access. An outage must never look like a cancellation."""

    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert _settled_license(monkeypatch)["cloud_access_active"] is True

    def _offline(*_args, **_kwargs):
        raise cloud_session.CloudSessionError(
            "Engraphis Cloud is temporarily unreachable.", status=503, transient=True
        )

    monkeypatch.setattr(cloud_session, "access_for_workspace", _offline)
    assert v2_api._fetch_authoritative_entitlement() is None

    record = cloud_session._load()
    assert record.get("cloud_access_active") is True, "an outage revoked a paying customer"
    assert "team" in record.get("cloud_features", [])


def test_a_lapsed_subscription_declared_on_the_refresh_keeps_its_name(
    monkeypatch,
) -> None:
    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(
        _entitlement_dto("team", active=False),
        registration=_registration_entitlement("team", active=False),
    ))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "team"
    assert payload["features"] == []
    assert payload["cloud_access_active"] is False


def test_a_downgrade_to_free_is_reported_as_the_local_core(monkeypatch) -> None:
    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(
        _entitlement_dto("free", active=False),
        registration=_registration_entitlement("free", active=False),
    ))

    payload = _settled_license(monkeypatch)

    assert payload["plan"] == "local"
    assert payload["features"] == []


@pytest.mark.parametrize("response", [
    {},
    {"plan": ""},
    {"plan": None},
    {"plan": 7},
    {"cloud_features": ["analytics"], "cloud_access_active": True},
])
def test_the_registration_entitlement_fields_are_optional(response) -> None:
    """An older control plane omits them; a partial body must not invent an answer."""

    assert cloud_session._declared_entitlement(response) == {}


def test_a_plan_without_an_activity_flag_is_not_treated_as_lapsed() -> None:
    """Absent is not ``False``: defaulting it would re-lock a paying customer."""

    declared = cloud_session._declared_entitlement({"plan": "Pro"})

    assert declared["plan"] == "pro"
    assert declared["cloud_access_active"] is True
    assert "cloud_features" not in declared
    assert cloud_session._declared_entitlement({"plan": "free"})[
        "cloud_access_active"] is False


def test_an_oversized_provider_entitlement_cannot_brick_the_saved_session() -> None:
    """The session record is read back under a 64 KiB cap; a huge value must be bounded.

    Growing the file past that cap would make ``_load`` raise on every subsequent call and
    take cloud access down with it, so the persisted presentation strings are trimmed.
    """

    declared = cloud_session._declared_entitlement({
        "plan": "team" + "x" * 100_000,
        "cloud_features": ["f%d" % index + "y" * 10_000 for index in range(5_000)],
        "cloud_access_active": True,
    })

    assert len(declared["plan"]) <= 64
    assert len(declared["cloud_features"]) <= 32
    assert all(len(feature) <= 64 for feature in declared["cloud_features"])
    assert len(json.dumps(declared)) < 8 * 1024


def test_a_session_plan_for_another_organization_is_refused(monkeypatch) -> None:
    """A pinned deployment may point at a different organization than it registered for.

    Serving the saved session's plan there would relabel one customer with another's.
    """

    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert _settled_license(monkeypatch)["plan_source"] == "session"

    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_somebody_else")

    assert v2_api._session_entitlement() == {}
    # Nothing was destroyed: the record is still correct for the organization it names.
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", ORGANIZATION)
    assert v2_api._session_entitlement()["plan"] == "team"


def test_a_cached_entitlement_for_another_organization_is_refused(monkeypatch) -> None:
    """The same guard, on the compatibility cache — which the session path already had.

    The cache file lives in the state directory, which outlives a reconnect and is shared
    by every organization the installation is ever pointed at. Serving it unchecked showed
    the PREVIOUS organization's Team badge and Team grant for a whole refresh interval, and
    for ever with the refresh disabled.
    """

    _connect(monkeypatch)
    path = v2_api._entitlement_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    v2_api._write_entitlement_cache({
        "plan": "team", "features": v2_api.entitled_features("team"),
        "cloud_access_active": True, "organization_id": ORGANIZATION,
        "fetched_at": time.time(),
    })
    assert v2_api._read_entitlement_cache()["plan"] == "team"

    # Re-pinned to somebody else's organization, reusing the very same state directory.
    # The kill switch models the worst case: nothing will ever correct a served stale answer.
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_somebody_else")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", "0")

    assert v2_api._read_entitlement_cache() == {}
    payload = v2_api.get_license()
    assert payload["plan_source"] != "cloud"
    assert "team" not in payload["features"], "another organization's Team grant was served"

    # Nothing was destroyed: the file is still correct for the organization it names.
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", ORGANIZATION)
    assert v2_api._read_entitlement_cache()["plan"] == "team"


def test_a_cached_entitlement_follows_a_reconnected_session_not_just_a_pin(
    monkeypatch,
) -> None:
    """No pin at all: reconnecting the installation rebinds it, and the cache must follow."""

    _connect(monkeypatch, pinned_token=False)
    cloud_session.save_bootstrap(
        {"refresh_credential": "engr_rt_bootstrap", "organization_id": ORGANIZATION,
         "installation_id": "inst_1", "device_id": "dev_1", "token_subject": "member"},
        control_url=CONTROL_URL, compute_url=CONTROL_URL,
    )
    v2_api._write_entitlement_cache({
        "plan": "team", "features": v2_api.entitled_features("team"),
        "cloud_access_active": True, "organization_id": ORGANIZATION,
        "fetched_at": time.time(),
    })
    assert v2_api._read_entitlement_cache()["plan"] == "team"

    # The installation is connected again, this time to a different organization.
    cloud_session.save_bootstrap(
        {"refresh_credential": "engr_rt_bootstrap", "organization_id": "org_second_tenant",
         "installation_id": "inst_1", "device_id": "dev_1", "token_subject": "member"},
        control_url=CONTROL_URL, compute_url=CONTROL_URL,
    )

    assert v2_api._read_entitlement_cache() == {}


def test_an_unreadable_session_yields_no_entitlement_rather_than_raising(
    monkeypatch,
) -> None:
    """``saved_entitlement`` is on the boot path and may never raise."""

    def _boom() -> dict:
        raise cloud_session.CloudSessionError("unsafe state file", status=409)

    monkeypatch.setattr(cloud_session, "_load", _boom)

    assert cloud_session.saved_entitlement() == {}
    assert v2_api._session_entitlement() == {}


# ── (5) precedence ────────────────────────────────────────────────────────────
def test_the_session_plan_outranks_the_cached_entitlement(monkeypatch) -> None:
    """Two persisted answers, one documented winner — never a silent disagreement."""

    _connect(monkeypatch, pinned_token=False)
    # The compatibility cache holds a stale Pro; the session holds the current Team.
    v2_api._write_entitlement_cache({
        "plan": "pro", "features": v2_api.entitled_features("pro"),
        "cloud_access_active": True, "organization_id": ORGANIZATION,
        "fetched_at": time.time(),
    })
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("pro"),
                                          registration=_registration_entitlement("team")))
    cloud_session.access_for_workspace(None, require_compute=False)

    payload = v2_api.get_license()

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "session"
    assert "team" in payload["features"]
    # The stale compatibility cache is untouched, simply outranked.
    assert v2_api._read_entitlement_cache()["plan"] == "pro"


def test_the_environment_override_outranks_the_session_plan(monkeypatch) -> None:
    _connect(monkeypatch, pinned_token=False)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team"),
                                          registration=_registration_entitlement("team")))
    assert _settled_license(monkeypatch)["plan_source"] == "session"

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "pro")
    payload = v2_api.get_license()

    assert payload["plan"] == "pro"
    assert payload["plan_source"] == "environment"


def test_the_environment_override_outranks_the_cached_entitlement(monkeypatch) -> None:
    """An air-gapped or pinned-token deployment must still be able to state its plan."""

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("pro")))
    assert _settled_license(monkeypatch)["plan"] == "pro"

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "team")
    payload = v2_api.get_license()

    assert payload["plan"] == "team"
    assert payload["plan_source"] == "environment"
    assert "team" in payload["features"]


@pytest.mark.parametrize("declared,expected", [
    ("pro", "pro"), ("team", "team"), ("free", "local"), ("TEAM", "team"), (" pro ", "pro"),
])
def test_the_override_vocabulary_is_unchanged(monkeypatch, declared, expected) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", declared)

    assert v2_api._hosted_plan() == expected


def test_an_unrecognised_override_falls_through_instead_of_locking_a_customer_out(
    monkeypatch,
) -> None:
    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "enterprise")

    assert _settled_license(monkeypatch)["plan"] == "team"


# ── (6) the two license surfaces must agree ───────────────────────────────────
def test_both_license_surfaces_report_the_same_plan(monkeypatch) -> None:
    """P2: ``/memory/license`` hardcoded ``local`` while ``/api/license`` reported Team."""

    import asyncio

    from engraphis.routes import memory as memory_routes

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    v2 = _settled_license(monkeypatch)
    v1 = asyncio.new_event_loop().run_until_complete(memory_routes.get_license())["data"]

    assert v1["plan"] == v2["plan"] == "team"
    assert v1["features"] == v2["features"]
    assert "team" in v1["features"]
    # The legacy envelope and its disclosure fields are unchanged.
    assert v1["cloud_managed"] is True
    assert v1["trial_seconds"] == 259_200
    assert v1["grace_seconds"] == 86_400
    assert v1["grace_extends_cloud_access"] is False
    assert v1["upgrade_url"]


def test_the_legacy_surface_still_reports_local_for_an_unconnected_installation() -> None:
    """The pinned v1 contract (tests/test_v1_licensing.py) describes *this* case."""

    import asyncio

    from engraphis.routes import memory as memory_routes

    v1 = asyncio.new_event_loop().run_until_complete(memory_routes.get_license())["data"]

    assert v1["plan"] == "local"
    assert v1["features"] == []


# ── (6b) the boot path resolves the plan once, and the override seam still works ──
def test_the_license_route_resolves_the_entitlement_exactly_once(monkeypatch) -> None:
    """``_plan_entitlement`` reads the cloud session and the entitlement cache off disk and
    can schedule a background refresh. ``hosted_plan_summary`` called it directly *and*
    again through ``_hosted_plan``, doubling all of that on every ``/api/license`` — and
    therefore on every ``/api/bootstrap``.
    """

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", "0")
    resolutions = []
    resolve = v2_api._plan_entitlement

    def _counted() -> dict:
        resolutions.append(1)
        return resolve()

    monkeypatch.setattr(v2_api, "_plan_entitlement", _counted)

    payload = v2_api.get_license()

    assert payload["plan"] == "pro"
    assert len(resolutions) == 1, "the plan was resolved %d times per request" % len(
        resolutions
    )


def test_one_resolution_also_covers_the_legacy_license_surface(monkeypatch) -> None:
    """``/memory/license`` renders the same summary, so it inherited the same double read."""

    import asyncio

    from engraphis.routes import memory as memory_routes

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", "0")
    reads = []
    read_cache = v2_api._read_entitlement_cache

    def _counted() -> dict:
        reads.append(1)
        return read_cache()

    monkeypatch.setattr(v2_api, "_read_entitlement_cache", _counted)

    asyncio.new_event_loop().run_until_complete(memory_routes.get_license())

    assert len(reads) == 1, "the entitlement cache was read %d times" % len(reads)


def test_a_replaced_hosted_plan_still_overrides_the_resolved_entitlement(
    monkeypatch,
) -> None:
    """The override seam is load-bearing (tests/test_client_launch.py patches it).

    Resolving once must not mean ignoring a caller that replaced ``_hosted_plan``: its
    answer still wins, and the feature list still falls back to this client's plan table.
    """

    _connect(monkeypatch)
    _serve(monkeypatch, _FakeControlPlane(_entitlement_dto("team")))
    assert _settled_license(monkeypatch)["plan"] == "team"

    monkeypatch.setattr(v2_api, "_hosted_plan", lambda: "local")
    payload = v2_api.get_license()

    assert payload["plan"] == "local"
    assert payload["features"] == []
    assert payload["plan_source"] == "override"
    assert payload["cloud_access_active"] is False

    monkeypatch.setattr(v2_api, "_hosted_plan", lambda: "team")
    upgraded = v2_api.get_license()

    assert upgraded["plan"] == "team"
    assert "team" in upgraded["features"]


# ── (7) the shipped modules must still build on the supported floor ───────────
@pytest.mark.parametrize("relative", [
    "engraphis/routes/v2_api.py",
    "engraphis/routes/memory.py",
    "engraphis/cloud_session.py",
])
def test_the_touched_modules_still_parse_on_python_39(relative) -> None:
    ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"), feature_version=(3, 9))
