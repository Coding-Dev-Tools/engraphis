"""Tests for the licensing + cloud_license compilation integrity guards and tamper detection."""

import sys

import pytest

import engraphis.licensing as lic


def test_guard_passes_when_no_compiled_extension():
    lic._verify_module_integrity()


def test_guard_raises_when_pyd_exists_alongside(monkeypatch, tmp_path):
    from importlib.machinery import EXTENSION_SUFFIXES
    py_path = tmp_path / "licensing.py"
    py_path.write_text("# stub")
    for suffix in EXTENSION_SUFFIXES:
        (tmp_path / ("licensing" + suffix)).write_text("")
        break
    monkeypatch.setattr(sys.modules["engraphis.licensing"], "__file__", str(py_path))
    with pytest.raises(lic.LicenseError, match="integrity check failed"):
        lic._verify_module_integrity()


def test_guard_passes_when_compiled_extension(monkeypatch):
    monkeypatch.setattr(lic, "__file__", "/path/engraphis/licensing.cp312-win_amd64.pyd")
    lic._verify_module_integrity()


# ── tamper detection ──

def test_tamper_detection_skipped_in_test_mode():
    try:
        lic.require_feature("nonexistent")
    except lic.LicenseError as exc:
        assert "nonexistent" in str(exc)
        assert "integrity" not in str(exc).lower()


def test_tamper_detects_replaced_has_feature(monkeypatch):
    monkeypatch.setattr(lic, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    lic._snapshot_critical_globals()
    monkeypatch.setattr(lic, "has_feature", lambda _: True)
    with pytest.raises(lic.LicenseError, match="has_feature"):
        lic.require_feature("nonexistent")


def test_tamper_detects_replaced_current_license(monkeypatch):
    monkeypatch.setattr(lic, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    lic._snapshot_critical_globals()
    monkeypatch.setattr(lic, "current_license", lambda **kw: lic.License(plan="team",
        features=frozenset({"analytics", "export", "automation", "team", "sync"})))
    with pytest.raises(lic.LicenseError, match="current_license"):
        lic.require_feature("nonexistent")


def test_production_warnings_returns_list():
    assert isinstance(lic.production_warnings(), list)


def test_upgrade_url_not_empty():
    assert len(lic.upgrade_url()) > 0
