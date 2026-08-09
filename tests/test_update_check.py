"""Offline tests for the update-reminder module (engraphis.update_check).

Everything here is deterministic and network-free: version math is pure, and the one
code path that would hit the network (``_fetch``) is either monkeypatched or exercised
only on inputs it rejects *before* opening a socket.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engraphis import update_check as u
from engraphis.private_state import UnsafeStateFile


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


@pytest.mark.parametrize("text", [
    "1." + "9" * 1000,
    ".".join(["1"] * (u._MAX_VERSION_PARTS + 1)),
])
def test_parse_version_rejects_pathological_numeric_versions(text):
    assert u.parse_version(text) is None


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
    got = u._parse_release_payload({"info": {"version": "1.5"}})
    assert got["version"] == "1.5"
    assert "1.5" in got["url"]


def test_parse_generic_and_garbage():
    assert u._parse_release_payload({"version": "3.0", "url": "https://x/y"}) == {
        "version": "3.0", "url": "https://x/y"}
    assert u._parse_release_payload({"nope": 1}) is None
    assert u._parse_release_payload("not a dict") is None


@pytest.mark.parametrize(
    "version",
    [
        "999.0\nforged",
        "999.0\x1b]52;clipboard\x07",
        {"major": 999},
        ["999", "0"],
        "9" * (u._MAX_VERSION_TEXT + 1),
    ],
)
def test_release_payload_rejects_non_display_safe_versions(version):
    assert u._parse_release_payload(
        {"version": version, "url": "https://example.test/release"}
    ) is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/release",
        "https://user@example.test/release",
        "https://example.test/release\nforged",
        "https://example.test/" + "x" * u._MAX_RELEASE_URL,
    ],
)
def test_release_payload_drops_unsafe_display_urls(url):
    assert u._parse_release_payload({"version": "9.9.9", "url": url}) == {
        "version": "9.9.9",
        "url": "",
    }


# ── network guard (no socket opened for a bad scheme/host) ────────────────────
@pytest.mark.parametrize("url", [
    "http://example.com/releases",   # plain http, non-loopback
    "ftp://example.com/x",
    "file:///etc/passwd",
    "https://user@example.com/releases",
    "https://[::1/releases",
    "https://example.com\\@127.0.0.1/releases",
])
def test_fetch_rejects_unsafe_schemes(url):
    assert u._fetch(url, timeout=0.01) is None


def test_fetch_rejects_dns_loopback_alias_before_opening(monkeypatch):
    monkeypatch.setattr(
        u, "build_pinned_https_opener",
        lambda *args, **kwargs: pytest.fail("a DNS alias must not reach an HTTP opener"),
    )
    assert u._fetch("http://localhost/latest", timeout=0.01) is None


# ── endpoint / explicit opt-in configuration ──────────────────────────────────
def test_endpoint_default_and_overrides(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_UPDATE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_UPDATE_REPO", raising=False)
    assert u._endpoint() == "https://api.github.com/repos/%s/releases/latest" % u.DEFAULT_REPO
    monkeypatch.setenv("ENGRAPHIS_UPDATE_REPO", "acme/thing")
    assert u._endpoint().endswith("/repos/acme/thing/releases/latest")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_URL", "https://mirror/latest.json")
    assert u._endpoint() == "https://mirror/latest.json"  # explicit URL wins over repo


@pytest.mark.parametrize("value", [
    None, "0", "false", "no", "off", "disable", "disabled",
    "treu", "enabled-ish", "2", "random",
])
def test_unset_false_like_and_misspelled_values_stay_offline(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ENGRAPHIS_UPDATE_CHECK", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", value)
    assert u.enabled() is False

    # Every non-affirmative value must keep check() from opening a socket.
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("must not hit network"))
    snap = u.check()
    assert snap == u._disabled_snapshot()
    assert u.notice_line(snap) is None


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "enable", "enabled"])
def test_recognized_explicit_opt_in_values(monkeypatch, value):
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", value)
    assert u.enabled() is True


# ── cache + snapshot behavior ─────────────────────────────────────────────────
@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Isolate the on-disk cache and force checks enabled with a known endpoint."""
    path = tmp_path / "update.json"
    monkeypatch.delenv("ENGRAPHIS_UPDATE_CACHE", raising=False)
    monkeypatch.setattr(u, "_cache_path", lambda: str(path))
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "1")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_URL", "https://example.test/latest")
    return path


def test_cache_setting_is_a_bounded_ttl_not_a_path(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(state))
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CACHE", str(victim))

    assert u._cache_ttl_seconds() == u.CACHE_TTL_SECONDS
    cache_path = u._cache_path()
    assert cache_path is not None
    assert Path(cache_path) == state / "update_check.json"
    u._write_cache("1.2.3", "https://example.test/release")

    assert victim.read_text(encoding="utf-8") == "keep"
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CACHE", "60")
    assert u._cache_ttl_seconds() == 60
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CACHE", "0")
    assert u._cache_ttl_seconds() == u.CACHE_TTL_SECONDS
    monkeypatch.setenv(
        "ENGRAPHIS_UPDATE_CACHE",
        str(u._MAX_CACHE_TTL_SECONDS + 1),
    )
    assert u._cache_ttl_seconds() == u.CACHE_TTL_SECONDS
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert f"# ENGRAPHIS_UPDATE_CACHE={u.CACHE_TTL_SECONDS}" in example
    assert str(u._MAX_CACHE_TTL_SECONDS) in example


def test_default_cache_directory_is_owner_private(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not authoritative on Windows")
    state = tmp_path / "state"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(state))
    monkeypatch.delenv("ENGRAPHIS_UPDATE_CACHE", raising=False)

    u._write_cache("1.2.3", "https://example.test/release")

    cache_path = Path(u._cache_path())
    assert cache_path.exists()
    assert state.stat().st_mode & 0o077 == 0
    assert cache_path.stat().st_mode & 0o077 == 0


def test_cache_write_is_fail_silent_when_private_directory_is_unsafe(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        u,
        "ensure_owner_private_dir",
        lambda _path: (_ for _ in ()).throw(UnsafeStateFile("unsafe")),
    )

    u._write_cache("1.2.3", "https://example.test/release")


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


def test_unexpected_fetch_failure_is_fail_silent(cache, monkeypatch):
    stale = {"latest": "1.4.0", "url": "https://rel/1.4.0", "checked_at": 0.0}
    cache.write_text(json.dumps(stale))

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider detail must not escape")

    monkeypatch.setattr(u, "_fetch", fail)
    snap = u.check()

    assert snap["latest"] == "1.4.0"
    assert snap["error"] == "update check unavailable"

def test_snapshot_is_non_blocking(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    called = {"bg": False}
    monkeypatch.setattr(u, "refresh_in_background", lambda *a, **k: called.__setitem__("bg", True))
    monkeypatch.setattr(u, "_fetch", lambda *a, **k: pytest.fail("snapshot must not fetch inline"))
    snap = u.snapshot()  # empty cache → returns immediately, schedules a background refresh
    assert snap["update_available"] is False
    assert called["bg"] is True


@pytest.mark.parametrize("checked_at", [
    [1], {"value": 1}, "nan", "inf", "-inf",
])
def test_malformed_cache_timestamp_is_fail_silent(cache, monkeypatch, checked_at):
    cache.write_text(json.dumps({"latest": "2.0.0", "checked_at": checked_at}))
    monkeypatch.setattr(u, "refresh_in_background", lambda *args, **kwargs: None)

    snap = u.snapshot()

    assert snap["checked_at"] == 0.0


def test_oversized_cache_is_ignored(cache):
    cache.write_text("x" * (u._MAX_CACHE_BYTES + 1))
    assert u._read_cache() == {}


def test_linked_cache_is_ignored_and_never_overwrites_target(cache):
    victim = cache.with_name("victim.json")
    victim.write_text("do not replace")
    try:
        cache.symlink_to(victim)
    except (NotImplementedError, OSError):
        try:
            os.link(victim, cache)
        except OSError:
            pytest.skip("this platform cannot create a link for the cache test")

    assert u._read_cache() == {}
    u._write_cache("9.9.9", "https://example.test/release")
    assert victim.read_text() == "do not replace"


def test_cached_and_direct_notice_values_are_sanitized(cache, monkeypatch):
    monkeypatch.setattr(u, "CURRENT_VERSION", "1.0.0")
    cache.write_text(
        json.dumps(
            {
                "latest": "9.9.9\nforged",
                "url": "https://example.test/release\nforged",
                "error": "unavailable\rforged",
                "checked_at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    snapshot = u._snapshot_from_cache(u._read_cache())
    assert snapshot["latest"] == ""
    assert snapshot["url"] == ""
    assert snapshot["error"] == ""
    assert snapshot["update_available"] is False
    assert u.notice_line(snapshot) is None
    assert u.notice_line(
        {
            "enabled": True,
            "update_available": True,
            "latest": "9.9.9\nforged",
            "current": "1.0.0",
            "url": "https://example.test/release",
        }
    ) is None


def test_notice_line(monkeypatch):
    line = u.notice_line({"enabled": True, "update_available": True,
                          "latest": "1.4.0", "current": "1.0.0", "url": "https://rel/1.4.0"})
    assert line is not None
    assert "1.4.0" in line and "1.0.0" in line and "pip install -U engraphis" in line
    assert u.notice_line({"enabled": True, "update_available": False}) is None


def test_cli_notice_uses_the_non_blocking_snapshot_and_is_fail_silent(monkeypatch):
    seen = []
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "1")
    monkeypatch.setattr(u, "snapshot", lambda: {
        "enabled": True, "update_available": True, "latest": "1.4.0", "current": "1.0.0",
        "url": "https://rel/1.4.0",
    })
    monkeypatch.setattr(u, "check", lambda **_kwargs: pytest.fail("CLI must not check inline"))
    u.emit_cli_notice(seen.append)
    assert seen and "1.4.0" in seen[0]

    monkeypatch.setattr(u, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    u.emit_cli_notice(seen.append)
    assert len(seen) == 1


def test_primary_ledger_renders_the_update_snapshot():
    root = __import__("pathlib").Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "ledger.js").read_text(encoding="utf-8")
    css = (root / "ledger.css").read_text(encoding="utf-8")
    assert 'id="update-banner"' in html
    assert "renderUpdateBanner(bootstrap.update)" in script
    assert "pip install -U engraphis" in script
    assert ".update-banner" in css


def test_api_update_endpoint(monkeypatch):
    pytest.importorskip("fastapi", reason="v2_api requires fastapi (extras)")
    from engraphis.routes import v2_api
    monkeypatch.setattr(u, "snapshot",
                        lambda: {"enabled": True, "update_available": True, "latest": "1.4.0"})
    out = v2_api.api_update(force=False)
    assert out["update_available"] is True and out["latest"] == "1.4.0"


def test_api_update_never_raises(monkeypatch):
    pytest.importorskip("fastapi", reason="v2_api requires fastapi (extras)")
    from engraphis.routes import v2_api

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(u, "snapshot", boom)
    out = v2_api.api_update(force=False)
    assert out == {"enabled": False, "update_available": False}
