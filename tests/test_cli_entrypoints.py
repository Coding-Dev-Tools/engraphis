"""Dependency-light help and invalid-invocation behavior for console shims."""
import argparse
import builtins
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from engraphis import mcp_cli
from scripts import approve_memory, inspector, start_dashboard, start_server


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_runtime_message_requires_newer_python(monkeypatch):
    monkeypatch.setattr(mcp_cli.sys, "version_info", (3, 9, 25))
    assert "Python 3.10 or newer" in mcp_cli._dependency_error()


def test_mcp_runtime_message_checks_optional_dependency(monkeypatch):
    monkeypatch.setattr(mcp_cli.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(mcp_cli.importlib.util, "find_spec", lambda _name: None)
    assert 'pip install "engraphis[mcp]"' in mcp_cli._dependency_error()


def test_help_paths_exit_zero_without_starting_servers():
    for main in (mcp_cli.main, start_server.main, inspector.main):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0


def test_server_and_retired_inspector_reject_unknown_arguments():
    for main in (start_server.main, inspector.main):
        with pytest.raises(SystemExit) as exc:
            main(["--definitely-invalid"])
        assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "65536", "bad"])
def test_server_port_validation(value):
    with pytest.raises(argparse.ArgumentTypeError):
        start_server._port(value)


def test_server_alias_starts_the_dashboard_headlessly(monkeypatch):
    captured = []
    monkeypatch.setattr(start_server.start_dashboard, "main", captured.append)

    start_server.main(["--reload"])

    assert captured == [["--reload", "--no-open"]]


def test_dashboard_missing_server_extra_does_not_print_db_path(monkeypatch, capsys):
    sensitive = "C:/private/operator/memory.db"
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", sensitive)
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: True)
    real_import = builtins.__import__

    def missing_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ModuleNotFoundError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_uvicorn)
    with pytest.raises(SystemExit) as exc:
        start_dashboard.main(["--no-open"])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert sensitive not in output.out + output.err
    assert 'pip install "engraphis[server]"' in output.err


def test_approval_cli_uses_configured_memory_service_factory(monkeypatch):
    captured = {}

    class FakeStore:
        def close(self):
            captured["closed"] = True

    class FakeEngine:
        def approve_for_prompt(self, memory_id, *, reviewer, reason):
            captured["approval"] = (memory_id, reviewer, reason)
            return {"id": "mem_approved"}

    class FakeService:
        store = FakeStore()
        engine = FakeEngine()

        @classmethod
        def create(cls, db_path, **kwargs):
            captured["create"] = (db_path, kwargs)
            return cls()

    output = []
    monkeypatch.setattr(approve_memory, "MemoryService", FakeService)
    monkeypatch.setattr(
        approve_memory,
        "settings",
        SimpleNamespace(
            db_path="configured-encrypted.db",
            embed_model="configured-embedder",
            embed_dim=768,
            rerank_model="configured-reranker",
            allowed_workspaces=["acme"],
        ),
    )
    monkeypatch.setattr(approve_memory.sys, "argv", [
        "approve_memory.py", "mem_pending", "--reason", "verified by owner",
    ])
    monkeypatch.setattr(approve_memory.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        approve_memory.sys,
        "stdout",
        SimpleNamespace(isatty=lambda: True, write=output.append, flush=lambda: None),
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: "APPROVE mem_pending")

    approve_memory.main()

    assert captured["create"] == (
        "configured-encrypted.db",
        {
            "embed_model": "configured-embedder",
            "embed_dim": 768,
            "rerank_model": "configured-reranker",
            "allowed_workspaces": ["acme"],
        },
    )
    assert captured["approval"] == ("mem_pending", approve_memory.getpass.getuser(), "verified by owner")
    assert captured["closed"] is True
    assert "mem_approved" in "".join(output)


def test_local_cli_ingest_is_recallable_across_clean_processes(tmp_path):
    """The local console is the owner-approved write boundary, not HTTP ingress.

    This reproduces the installed-wheel failure mode without network or model downloads:
    each command starts a fresh process against one explicitly configured SQLite file.
    """
    environment = {
        **os.environ,
        "ENGRAPHIS_DB_PATH": str(tmp_path / "cli.db"),
        "ENGRAPHIS_EMBED_MODEL": "",
        "ENGRAPHIS_EXTRACTOR": "none",
        "ENGRAPHIS_GRAPH_EXTRACTOR": "none",
        "ENGRAPHIS_UPDATE_CHECK": "0",
    }
    ingest = subprocess.run(
        [sys.executable, "-m", "scripts.cli", "ingest", "The release is blue.", "-n", "ops"],
        cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30, check=False,
    )
    recall = subprocess.run(
        [sys.executable, "-m", "scripts.cli", "recall", "blue", "-n", "ops"],
        cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30, check=False,
    )

    assert ingest.returncode == 0, ingest.stderr
    assert "Stored:" in ingest.stdout
    assert recall.returncode == 0, recall.stderr
    assert "Found 1 memories:" in recall.stdout
    assert "The release is blue." in recall.stdout
