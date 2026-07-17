"""Launcher configuration regressions."""

from scripts import start_dashboard


def test_embed_model_uses_default_only_when_unset(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_MODEL", raising=False)
    assert start_dashboard._embed_model_from_environment() == "sentence-transformers/all-MiniLM-L6-v2"


def test_embed_model_preserves_explicit_offline_opt_out(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    assert start_dashboard._embed_model_from_environment() == ""
