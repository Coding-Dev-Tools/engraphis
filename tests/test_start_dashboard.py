"""Launcher configuration regressions."""

import argparse
import sys
import types

import pytest

from scripts import start_dashboard


def test_embed_model_uses_default_only_when_unset(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_MODEL", raising=False)
    assert start_dashboard._embed_model_from_environment() == "sentence-transformers/all-MiniLM-L6-v2"


def test_embed_model_preserves_explicit_offline_opt_out(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    assert start_dashboard._embed_model_from_environment() == ""


@pytest.mark.parametrize("value", ["0", "-1", "65536", "not-a-number"])
def test_port_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        start_dashboard._port(value)


def test_port_accepts_boundaries():
    assert start_dashboard._port("1") == 1
    assert start_dashboard._port("65535") == 65535


def test_launcher_preserves_socket_peer_for_forwarded_header_validation(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    fake = types.ModuleType("engraphis.dashboard_app")
    fake.app = object()
    monkeypatch.setitem(sys.modules, "engraphis.dashboard_app", fake)
    start_dashboard.main(["--no-open"])
    assert captured["proxy_headers"] is False
    assert "forwarded_allow_ips" not in captured
