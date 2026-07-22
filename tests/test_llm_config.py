"""Offline configuration coverage for the dashboard's LLM extractor switch."""
import os

import pytest

from engraphis.config import Settings, persist_project_env
from engraphis.private_state import UnsafeStateFile


def _adversarial_link(target, link):
    try:
        link.symlink_to(target)
        return "symlink"
    except (NotImplementedError, OSError):
        os.link(str(target), str(link))
        return "hardlink"


def test_persist_project_env_updates_only_requested_settings(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "KEEP_THIS=value\n"
        "ENGRAPHIS_EXTRACTOR=none\n"
        "export ENGRAPHIS_LLM_AUTO_EXTRACT=0\n",
        encoding="utf-8",
    )

    persist_project_env({
        "ENGRAPHIS_EXTRACTOR": "llm_structured",
        "ENGRAPHIS_LLM_AUTO_EXTRACT": "1",
    }, path=target)

    saved = target.read_text(encoding="utf-8")
    assert "KEEP_THIS=value" in saved
    assert "ENGRAPHIS_EXTRACTOR=llm_structured" in saved
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=1" in saved
    assert "export ENGRAPHIS_LLM_AUTO_EXTRACT" not in saved


def test_persist_project_env_keeps_private_mode_and_cleans_temporary_files(tmp_path):
    target = tmp_path / ".env"
    target.write_text("PRIVATE=value\n", encoding="utf-8")
    os.chmod(target, 0o600)

    persist_project_env({"ENGRAPHIS_EXTRACTOR": "none"}, path=target)

    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("..env.tmp-*"))


@pytest.mark.parametrize("values", [
    {"lowercase": "value"},
    {"ENGRAPHIS_EXTRACTOR": "bad\nvalue"},
])
def test_persist_project_env_rejects_unsafe_assignments(tmp_path, values):
    with pytest.raises(ValueError):
        persist_project_env(values, path=tmp_path / ".env")


def test_persist_project_env_rejects_link_without_reading_or_overwriting_target(tmp_path):
    victim = tmp_path / "outside.env"
    victim.write_text("SECRET=must-stay-private\n", encoding="utf-8")
    target = tmp_path / ".env"
    _adversarial_link(victim, target)

    with pytest.raises(UnsafeStateFile):
        persist_project_env({"ENGRAPHIS_EXTRACTOR": "none"}, path=target)
    assert victim.read_text(encoding="utf-8") == "SECRET=must-stay-private\n"
    assert target.exists()


def test_persist_project_env_ignores_predictable_legacy_temp_link(tmp_path):
    victim = tmp_path / "victim.env"
    victim.write_text("DO_NOT_TOUCH=1\n", encoding="utf-8")
    target = tmp_path / ".env"
    legacy_temp = tmp_path / "..env.tmp-1234-fixed"
    _adversarial_link(victim, legacy_temp)

    persist_project_env({"ENGRAPHIS_EXTRACTOR": "none"}, path=target)

    assert target.read_text(encoding="utf-8") == "ENGRAPHIS_EXTRACTOR=none\n"
    assert victim.read_text(encoding="utf-8") == "DO_NOT_TOUCH=1\n"
    assert legacy_temp.exists()


def test_persist_project_env_rejects_oversized_existing_file(tmp_path):
    target = tmp_path / ".env"
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(UnsafeStateFile, match="exceeds"):
        persist_project_env({"ENGRAPHIS_EXTRACTOR": "none"}, path=target)


def test_llm_auto_extract_defaults_off_and_accepts_explicit_on(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_LLM_AUTO_EXTRACT", raising=False)
    assert Settings().llm_auto_extract is False
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "1")
    assert Settings().llm_auto_extract is True
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "off")
    assert Settings().llm_auto_extract is False
