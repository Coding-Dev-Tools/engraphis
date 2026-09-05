"""Explicit install profiles preserve capability choices without guessing wheel metadata."""
import json

import pytest

from scripts import installation_profile as profiles
from scripts import update


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    path = tmp_path / "private" / "profile.json"
    monkeypatch.setattr(profiles, "profile_path", lambda _config_path=None: path)
    monkeypatch.delenv("ENGRAPHIS_UPDATE_EXTRAS", raising=False)
    return path


@pytest.mark.parametrize("extras,expected", [([], ""), (["mcp"], "[mcp]"), (["all"], "[all]"),
                                             (["mcp", "server"], "[mcp,server]")])
def test_update_preserves_explicit_profile(extras, expected):
    profiles.write_profile(extras)
    assert profiles.read_profile() == extras
    assert update._installed_extras() == expected
    assert update._explicit_installation_extras() == expected


def test_explicit_environment_override_wins(monkeypatch):
    profiles.write_profile(["server"])
    monkeypatch.setenv("ENGRAPHIS_UPDATE_EXTRAS", "none")
    assert update._installed_extras() == ""


def test_missing_or_invalid_profile_retains_legacy_fallback(isolated_profile):
    assert update._installed_extras() == "[all]"
    profiles.write_profile(["mcp"])
    isolated_profile.write_text("not JSON")
    assert profiles.read_profile() is None
    assert update._installed_extras() == "[all]"


def test_profile_cannot_select_extras_for_another_python_environment(isolated_profile):
    profiles.write_profile(["mcp"])
    data = json.loads(isolated_profile.read_text())
    data["environment"] = "/another/python"
    isolated_profile.write_text(json.dumps(data))
    assert profiles.read_profile() is None


def test_profile_rejects_unsafe_or_unvalidated_package_arguments(isolated_profile):
    profiles.write_profile(["mcp"])
    data = json.loads(isolated_profile.read_text())
    data["extras"] = ["--index-url=untrusted"]
    isolated_profile.write_text(json.dumps(data))
    assert profiles.read_profile() is None
