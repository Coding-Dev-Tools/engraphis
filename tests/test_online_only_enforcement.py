"""Online-only license enforcement — regression tests for the license-bypass fixes.

Covers the two headline bypasses that were closed:
  * BYPASS A — the old offline/local Pro trial was a forgeable, permanent, no-purchase
    Pro grant (its HMAC key was derivable from the public vendor key + local machine id).
    There is no local trial grant anymore, so a hand-written/forged trial file grants
    nothing.
  * BYPASS B — a signature-valid key used to unlock features OFFLINE (no server, no
    revocation, no seat cap). Enforcement is now online-only and fail-closed: every paid
    key must hold a live vendor lease.

These are pure-stdlib (like tests/test_licensing.py), stubbing the vendor server with a
monkeypatched ``cloud_license.register`` so no network is touched.
"""
import json
import time

import pytest

from engraphis import cloud_license as cl
from engraphis import licensing as lic
from engraphis.licensing import (
    License, compose_key, current_license, ed25519_public_key, has_feature, parse_key,
)

SECRET = bytes(range(32))  # deterministic test vendor keypair (matches test_licensing)

# Exercises the real server-side license gate — opt out of conftest's approve stub.
pytestmark = pytest.mark.real_license_gate


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    # Pin the test vendor verify key (honored only because conftest set the test-mode
    # override). Reroute the client-side cloud lease/device state to tmp for isolation.
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    # Blank the default relay so _cloud_gate has no server URL to fall back on —
    # tests that WANT a server must call _grant() which stubs cl.register.
    from engraphis import config as _cfg
    monkeypatch.setattr(_cfg.settings, "relay_url", "")
    monkeypatch.setattr(cl, "_DIR", tmp_path)
    monkeypatch.setattr(cl, "_LEASE_FILE", tmp_path / "lease.sig")
    monkeypatch.setattr(cl, "_MACHINE_ID_FILE", tmp_path / "machine_id")
    cl._machine_id_cache.clear()
    # Force a fresh read — the fixture runs BEFORE the test sets ENGRAPHIS_LICENSE_KEY,
    # so clear the cache completely so the test's own key is read when has_feature/license_error
    # call current_license().
    lic._cached = None
    lic._cache_error = ""
    lic._cache_recheck_at = 0
    yield
    lic._cached = None
    lic._cache_error = ""
    lic._cache_recheck_at = 0


def _key(plan="pro", days=365, cloud=True, trial=False, seats=1):
    now = int(time.time())
    payload = {"v": 1, "plan": plan, "email": "b@x.co", "seats": seats,
               "issued": now, "expires": now + days * 86400}
    if trial:
        payload["trial"] = 1
    if cloud:                       # a key minted with the server URL signed in
        payload["enforce"] = "cloud"
        payload["cloud_url"] = "http://vendor.test"
    return compose_key(payload, SECRET)


def _grant(monkeypatch):
    """Stub the vendor server to APPROVE: return a valid signed lease for the key+device."""
    def _register(base, key, mid, **kw):
        try:
            L = parse_key(key)
        except Exception:
            return None
        now = int(time.time())
        return cl.compose_lease(
            {"v": 1, "key_id": L.key_id, "plan": L.plan, "features": sorted(L.features),
             "machine_id": mid, "issued": now, "expires": now + 3600}, SECRET)
    monkeypatch.setattr(cl, "register", _register)


def _deny(monkeypatch):
    """Stub the vendor server as unreachable / refusing (revoked, seat limit, offline)."""
    monkeypatch.setattr(cl, "register", lambda *a, **k: None)


# ── bypass A: no local trial grant anymore ───────────────────────────────────────────

def test_no_key_is_free_tier():
    assert current_license(refresh=True) == License.free()
    assert not has_feature("analytics")


def test_forged_local_trial_file_grants_nothing(monkeypatch):
    """The retired local trial used an HMAC derivable from public data, so a user could
    forge trial.json for permanent Pro. Entitlement no longer reads any local trial file,
    so even a correctly-HMAC'd, far-future trial file grants nothing."""
    payload = {"started": 1, "expires": int(time.time() + 9_000_000),
               "trial_days": 9999}
    lic._TRIAL_FILE.write_text(json.dumps({"data": payload, "sig": lic._sign_trial(payload)}))
    assert current_license(refresh=True) == License.free()
    assert not has_feature("analytics")
    assert not has_feature("sync")


# ── bypass B: online-only, fail-closed ────────────────────────────────────────────────

def test_paid_key_denied_without_server(monkeypatch):
    _deny(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key())
    assert not has_feature("analytics")
    assert lic.license_error()          # a reason is recorded


def test_offline_style_key_also_requires_server(monkeypatch):
    """A key with NO enforce/cloud_url baked in (old offline style) must ALSO go through
    the server now — the default relay URL resolves, and with no lease it fails closed."""
    _deny(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key(cloud=False))
    assert not has_feature("analytics")


def test_paid_key_unlocks_with_valid_lease(monkeypatch):
    _grant(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key())
    assert has_feature("analytics") and has_feature("sync") and has_feature("export")
    assert current_license().plan == "pro"


def test_cached_lease_survives_server_outage(monkeypatch):
    """Once a lease is issued it is verified locally within its TTL — the 24h offline
    grace window — so a brief outage does not lock a paying customer out."""
    _grant(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key())
    assert has_feature("analytics")                       # obtains + caches the lease
    # A transient outage returns no lease but is not an authoritative denial; the cached
    # signed lease remains valid offline grace until its TTL expires.
    monkeypatch.setattr(cl, "register", lambda *a, **k: None)
    lic._cached = None
    lic._cache_recheck_at = 0
    assert has_feature("analytics")                       # served from the cached lease


# ── server-issued trial key ───────────────────────────────────────────────────────────

def test_trial_key_is_flagged_and_gated_like_a_purchase(monkeypatch):
    _grant(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key(plan="pro", days=3, trial=True))
    parsed = current_license(refresh=True)
    pub = parsed.to_public_dict()
    assert parsed.is_trial and pub["is_trial"]
    assert has_feature("analytics")
    assert pub["trial"]["active"] and pub["trial"]["days_left"] > 0


def test_trial_key_denied_without_server(monkeypatch):
    """Even a trial key is a real credential that must be leased — no server, no unlock."""
    _deny(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key(plan="pro", days=3, trial=True))
    assert not has_feature("analytics")


def test_request_trial_key_posts_plan(monkeypatch):
    """The client's trial request carries the plan so the relay mints the right tier."""
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"key": "ENGR1.fake", "days": 3}).encode()

    def _urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured.update(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(cl.urllib.request, "urlopen", _urlopen)
    key, reason, pending = cl.request_trial_key(
        "http://vendor.test", "mid-1", plan="pro", email="a@b.co")
    assert key == "ENGR1.fake" and pending is False
    assert captured["plan"] == "pro"
    assert captured["url"].endswith("/license/v1/start-trial")


# ── recheck cadence: every paid license re-validates against the server ───────────────

def test_every_paid_license_gets_rolling_recheck():
    now = 1_000.0
    assert lic._license_recheck_at(License.free(), now=now) == float("inf")
    assert lic._license_recheck_at(
        License(plan="pro"), now=now) == now + lic._CLOUD_RECHECK_SECONDS
    assert lic._license_recheck_at(
        License(plan="team", expires=now + 30), now=now) == now + 30