"""Tests for the licensing + cloud_license compilation integrity guards."""

import sys

import pytest

import engraphis.licensing as lic


# ── integrity guard ──

def test_guard_skipped_in_dev_mode():
    """ENGRAPHIS_DEV=1 is set by conftest.py — the guard does nothing."""
    lic._verify_module_integrity()


def test_guard_passes_when_running_as_py_without_pyd():
    """On our dev machine (no compiled .pyd/.so alongside licensing.py), the guard
    passes — it's the expected fallback for pure-python installs."""
    lic._verify_module_integrity()


def test_guard_raises_when_pyd_exists_alongside(monkeypatch, tmp_path):
    """If a .pyd file exists next to licensing.py, the guard fires because the
    compiled extension should have been loaded instead."""
    from importlib.machinery import EXTENSION_SUFFIXES
    monkeypatch.delenv("ENGRAPHIS_DEV", raising=False)
    py_path = tmp_path / "licensing.py"
    py_path.write_text("# stub")
    # Create a file matching any extension suffix (e.g. .pyd or .cp311-win_amd64.pyd)
    for suffix in EXTENSION_SUFFIXES:
        (tmp_path / ("licensing" + suffix)).write_text("")
        break  # one is enough for the guard to fire

    monkeypatch.setattr(sys.modules["engraphis.licensing"], "__file__", str(py_path))
    with pytest.raises(lic.LicenseError, match="integrity check failed"):
        lic._verify_module_integrity()


def test_guard_passes_when_compiled_extension(monkeypatch):
    """When __file__ ends in .pyd, the guard returns immediately without checking
    for a sibling .py."""
    monkeypatch.delenv("ENGRAPHIS_DEV", raising=False)
    monkeypatch.setattr(lic, "__file__", "/path/engraphis/licensing.cp312-win_amd64.pyd")
    lic._verify_module_integrity()


# ── production warnings ──

def test_production_warnings_returns_list():
    assert isinstance(lic.production_warnings(), list)


def test_upgrade_url_not_empty():
    assert len(lic.upgrade_url()) > 0
