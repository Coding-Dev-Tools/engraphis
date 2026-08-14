"""Regression tests for v1.6.1 security hotfix (SEC-001, pypdf CVEs)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest


def test_service_resolve_import_root_error_does_not_echo_path():
    """SEC-001: ValidationError messages must not contain the raw path."""
    from engraphis.service import _resolve_import_root, ValidationError

    with pytest.raises(ValidationError) as exc_info:
        _resolve_import_root("/some/secret/attacker/path")

    error_str = str(exc_info.value).lower()
    assert "/some/secret/attacker/path" not in error_str
    assert "secret" not in error_str
    assert "attacker" not in error_str


def test_pypdf_minimum_version():
    """Verify pypdf>=6.15.0 is declared to patch PYSEC-2026-3655/3656."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    pypdf_lines = [line for line in pyproject.splitlines() if "pypdf" in line.lower()]
    assert pypdf_lines, "pypdf dependency not found in pyproject.toml"
    for line in pypdf_lines:
        match = re.search(r'pypdf[>=<]+([0-9.]+)', line)
        if match:
            version = match.group(1)
            major, minor = map(int, version.split('.')[:2])
            assert (major, minor) >= (6, 15), f"pypdf {version} < 6.15.0 in: {line}"
