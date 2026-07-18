"""Dependency-light help and invalid-invocation behavior for console shims."""
import argparse
import builtins

import pytest

from engraphis import mcp_cli
from scripts import inspector, start_dashboard, start_server


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


def test_dashboard_missing_server_extra_does_not_print_db_path(monkeypatch, capsys):
    sensitive = "C:/private/operator/memory.db"
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", sensitive)
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
