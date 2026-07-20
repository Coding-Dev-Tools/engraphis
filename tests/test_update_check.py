"""Offline tests for the update-reminder module (engraphis.update_check).

Everything here is deterministic and network-free: version math is pure, and the one
code path that would hit the network (``_fetch``) is either monkeypatched or exercised
only on inputs it rejects *before* opening a socket.
"""
from __future__ import annotations

import json

import pytest

from engraphis import update_check as u


# ── pure version math ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("  V2.0 ", (2, 0)),
    ("1.2.3-rc1", (1, 2, 3)),
    ("1.0.0+build.5", (1, 0, 0)),
    ("10.4", (10, 4)),
    ("nightly", None),
    ("", None),
    (None, None),
    (123, None),
])
def test_parse_version(text, expected):
    assert u.parse_version(text) == expected


@pytest.mark.parametrize("latest,current,newer", [
    ("1.1.0", "1.0.0", True),
    ("1.0.1", "1.0.0", True),
    ("2.0", "1.9.9", True),
    ("1.0.0", "1.0.0", False),     # equal is not newer
    ("1.0", "1.0.0", False),       # zero-padded equal
    ("0.9.9", "1.0.0", False),
    ("v1.2.0", "1.1.5", True),     # tolerates the v prefix on both sides
    ("garbage", "1.0.0", False),   # unparseable → never newer
    ("1.0.0", "garbage", False),
])
def test_is_newer(latest, current, newer):
    assert u.is_newer(latest, current) is newer


# ── payload normalization ─────────────────────────────────────────────────────
def test_parse_github_release():
    got = u._parse_release_payload({
        "tag_name": "v1.4.0", "html_url": "https://example/releases/tag/v1.4.0",
        "draft": False, "prerelease": False,
    })
    assert got == {"version": "v1.4.0", "url": "https://example/releases/tag/v1.4.0"}


def test_parse_github_rejects_draft_and_prerelease():
    assert u._parse_release_payload({"tag_name": "v2", "draft": True}) is None
    assert u._parse_release_payload({"tag_name": "v2", "prerelease": True}) is None


def test_parse_pypi_payload():
    got = u._parse_release_payload({"info": {"version": "1.5.0"}})
    assert got["version"] == "1.5.0"
    assert "1.5.0" in got["url"]


def test_parse_generic_and_garbage():
    assert u._parse_release_payload({"version": "3.0", "url": "https://x/y"}) == {
        "version": "3.0", "url": "https://x/y"}
    assert u._parse_release_payload({"nope": 1}) is None
    assert u._parse_release_payload("not a dict") is None


# ── network guard (no socket opened for a bad scheme/host) ────────────────────
@pytest.mark.parametrize("url", [
    "http://example.com/releases",   # plain http, non-loopback
    "ftp://example.com/x",
    "file:///etc/passwd",
])
def test_fetch_rejects_unsafe_schemes(url):
    assert u._fetch(url, timeout=0.01) is None


# ── endpoint / opt-out configuration ──────────────────────────────────────────
def test_endpoint_default_and_overrides(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_UPDATE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_UPDATE_REPO", raising=False)
    assert u._endpoint() == "https://api.github.com/repos/%s/releases/latest" % u.DEFAULT_REPO
    monkeypatch.setenv("ENGRAPHIS_UPDATE_REPO", "acme/thing")
    assert u._endpoint().endswith("/repos/acme/thing/releases/latest")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_URL", "https://mirror/latest.json")
    assert u._endpoint() == "https://mirror/latest.json"  # explicit URL wins over repo


def test_disabled_opt_out(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "0")
    assert u.enabled() is False
    # A hard failure if any network is attempted while disabled.
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("must not hit network"))
    snap = u.check()
    assert snap == u._disabled_snapshot()
    assert snap["enabled"] is False and snap["update_available"] is False
    assert u.notice_line(snap) is None


# ── cache + snapshot behavior ─────────────────────────────────────────────────
@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Isolate the on-disk cache and force checks enabled with a known endpoint."""
    path = tmp_path / "update.json"
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CACHE", str(path))
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "1")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_URL", "https://example.test/latest")
    return path


def test_check_fetches_writes_cache_and_reports_update(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    monkeypatch.setattr(u, "_fetch",
                        lambda url, timeout: {"version": "1.4.0", "url": "https://rel/1.4.0"})
    snap = u.check(force=True)
    assert snap["update_available"] is True
    assert snap["latest"] == "1.4.0" and snap["current"] == "1.0.0"
    assert snap["url"] == "https://rel/1.4.0"
    # cache persisted
    saved = json.loads(cache.read_text())
    assert saved["latest"] == "1.4.0" and saved["checked_at"] > 0


def test_fresh_cache_short_circuits_network(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    u._write_cache("1.3.0", "https://rel/1.3.0")
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("fresh cache must not refetch"))
    snap = u.check()  # not forced → should use the fresh cache
    assert snap["latest"] == "1.3.0" and snap["update_available"] is True


def test_upgrade_clears_banner_without_ttl_wait(cache, monkeypatch):
    """After the user upgrades, a still-fresh cache whose ``latest`` == installed version
    must report no update — update_available is recomputed against the live version."""
    u._write_cache("1.4.0", "https://rel/1.4.0")
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.4.0")  # simulate the just-installed upgrade
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("no network needed"))
    snap = u.check()
    assert snap["update_available"] is False


def test_fetch_failure_preserves_last_good(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    u._write_cache("1.4.0", "https://rel/1.4.0")
    # Expire the cache so check() attempts a refresh, then have the network fail.
    stale = json.loads(cache.read_text())
    stale["checked_at"] = 0.0
    cache.write_text(json.dumps(stale))
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: None)
    snap = u.check()
    assert snap["latest"] == "1.4.0" and snap["update_available"] is True  # last good kept


def test_snapshot_is_non_blocking(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    called = {"bg": False}
    monkeypatch.setattr(u, "refresh_in_background", lambda *a, **k: called.__setitem__("bg", True))
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("snapshot must not fetch inline"))
    snap = u.snapshot()  # empty cache → returns immediately, schedules a background refresh
    assert snap["update_available"] is False
    assert called["bg"] is True


def test_notice_line(monkeypatch):
    line = u.notice_line({"enabled": True, "update_available": True,
                          "latest": "1.4.0", "current": "1.0.0", "url": "https://rel/1.4.0"})
    assert "1.4.0" in line and "1.0.0" in line and "pip install -U engraphis" in line
    assert u.notice_line({"enabled": True, "update_available": False}) is None


# ── API endpoint wrapper (fail-silent, offline) ───────────────────────────────
def test_api_update_endpoint(monkeypatch):
    pytest.importorskip("fastapi")
    from engraphis.routes import v2_api
    monkeypatch.setattr(u, "snapshot",
                        lambda: {"enabled": True, "update_available": True, "latest": "1.4.0"})
    out = v2_api.api_update(force=False)
    assert out["update_available"] is True and out["latest"] == "1.4.0"


def test_api_update_never_raises(monkeypatch):
    pytest.importorskip("fastapi")
    from engraphis.routes import v2_api

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(u, "snapshot", boom)
    out = v2_api.api_update(force=False)
    assert out == {"enabled": False, "update_available": False}
