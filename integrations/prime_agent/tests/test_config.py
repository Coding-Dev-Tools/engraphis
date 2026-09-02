from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from engraphis_prime_agent.config import (
    DEFAULT_AGENT_NAMES,
    EngraphisRuntimeConfig,
    _engraphis_environment,
    _non_blank,
    build_runtime_config,
)


def test_non_blank_trims_and_rejects_empty() -> None:
    assert _non_blank(None) is None
    assert _non_blank("") is None
    assert _non_blank("   ") is None
    assert _non_blank("  hello  ") == "hello"


def test_engraphis_environment_allowlist() -> None:
    env = {
        "ENGRAPHIS_DB_PATH": "/tmp/x.db",
        "ENGRAPHIS_WORKSPACE": "demo",
        "PATH": "/usr/bin",
        "Path": "C:\\Windows",
        "SystemRoot": "C:\\Windows",
        "ComSpec": "C:\\Windows\\System32\\cmd.exe",
        "ANTHROPIC_API_KEY": "sk-secret",
        "HOME": "/root",
        "USER": "alice",
    }
    forwarded = _engraphis_environment(env)
    assert set(forwarded) == {
        "ENGRAPHIS_DB_PATH",
        "ENGRAPHIS_WORKSPACE",
        "PATH",
        "Path",
        "SystemRoot",
        "ComSpec",
    }
    assert forwarded["ENGRAPHIS_DB_PATH"] == "/tmp/x.db"
    assert "ANTHROPIC_API_KEY" not in forwarded
    assert "HOME" not in forwarded


def test_engraphis_environment_ignores_non_string_values() -> None:
    env = {"ENGRAPHIS_WORKSPACE": 123, "PATH": None}  # type: ignore[dict-item]
    assert _engraphis_environment(env) == {}


def test_build_runtime_config_defaults() -> None:
    cfg = build_runtime_config(env={})
    assert cfg.command == "engraphis-mcp"
    assert cfg.args == ()
    assert cfg.cwd is None
    assert cfg.default_workspace is None
    assert cfg.default_repo is None
    assert cfg.environment == {}


def test_build_runtime_config_reads_env() -> None:
    env = {
        "ENGRAPHIS_MCP_COMMAND": "C:/venv/Scripts/engraphis-mcp.exe",
        "ENGRAPHIS_WORKSPACE": "engraphis",
        "ENGRAPHIS_REPO": "prime-agent",
        "ENGRAPHIS_DB_PATH": "C:/data/x.db",
        "ANTHROPIC_API_KEY": "sk-secret",
    }
    cfg = build_runtime_config(env=env)
    assert cfg.command == "C:/venv/Scripts/engraphis-mcp.exe"
    assert cfg.default_workspace == "engraphis"
    assert cfg.default_repo == "prime-agent"
    assert "ANTHROPIC_API_KEY" not in cfg.environment
    assert cfg.environment["ENGRAPHIS_DB_PATH"] == "C:/data/x.db"


def test_build_runtime_config_command_override() -> None:
    cfg = build_runtime_config(env={}, command="/abs/engraphis-mcp")
    assert cfg.command == "/abs/engraphis-mcp"


def test_build_runtime_config_trims_blank_env() -> None:
    env = {"ENGRAPHIS_WORKSPACE": "  ", "ENGRAPHIS_REPO": "  real  "}
    cfg = build_runtime_config(env=env)
    assert cfg.default_workspace is None
    assert cfg.default_repo == "real"


def test_default_agent_names_are_eight() -> None:
    assert len(DEFAULT_AGENT_NAMES) == 8
    assert "researcher" in DEFAULT_AGENT_NAMES
    assert "coder" in DEFAULT_AGENT_NAMES
    assert all(isinstance(name, str) and name for name in DEFAULT_AGENT_NAMES)
    # Names must be unique (default fleet keys must be hashable).
    assert len(set(DEFAULT_AGENT_NAMES)) == 8


def test_runtime_config_is_frozen() -> None:
    cfg = EngraphisRuntimeConfig()
    try:
        cfg.command = "x"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("EngraphisRuntimeConfig should be frozen")


def test_runtime_config_frozen_raises_frozen_instance_error_on_every_field() -> None:
    """Every public field must reject assignment with FrozenInstanceError."""
    cfg = EngraphisRuntimeConfig(
        command="x",
        args=("a", "b"),
        cwd="C:/work",
        default_workspace="ws",
        default_repo="repo",
        environment={"ENGRAPHIS_DB_PATH": "/tmp/x.db"},
    )
    for name in ("command", "args", "cwd", "default_workspace", "default_repo", "environment"):
        with pytest.raises(FrozenInstanceError):
            setattr(cfg, name, "mutated")  # type: ignore[misc]


def test_runtime_config_field_names_are_stable() -> None:
    """Lock the public dataclass surface so a refactor that renames a field
    is caught here rather than at a downstream caller."""
    expected = {
        "command",
        "args",
        "cwd",
        "default_workspace",
        "default_repo",
        "environment",
    }
    assert {f.name for f in fields(EngraphisRuntimeConfig)} == expected


def test_build_runtime_config_preserves_args_tuple_type() -> None:
    """`args` must remain a tuple — the stdio gateway expects a sequence and
    downstream code (e.g. ``list(self._config.args)``) relies on tuple semantics."""
    src_args = ("--flag", "value", "C:/path/with space")
    cfg = build_runtime_config(env={}, args=src_args)
    assert isinstance(cfg.args, tuple)
    assert cfg.args == src_args
    # Mutating the original tuple must not leak into the config.
    assert cfg.args is not src_args or cfg.args == src_args


def test_build_runtime_config_empty_args_default_to_empty_tuple() -> None:
    """The default is an empty tuple, not None or a list, so callers can
    iterate without a None-check."""
    cfg = build_runtime_config(env={})
    assert cfg.args == ()
    assert isinstance(cfg.args, tuple)


def test_engraphis_environment_handles_windows_specific_keys() -> None:
    """SystemRoot and ComSpec must be forwarded on Windows. We don't assume
    Windows-only — any platform that has these keys in env should see them
    through the allowlist."""
    env = {
        "SystemRoot": "C:\\Windows",
        "ComSpec": "C:\\Windows\\System32\\cmd.exe",
        "PATHEXT": ".EXE;.BAT",  # NOT in the allowlist; must be dropped.
        "WINDIR": "C:\\Windows",  # NOT in the allowlist; must be dropped.
    }
    forwarded = _engraphis_environment(env)
    assert forwarded["SystemRoot"] == "C:\\Windows"
    assert forwarded["ComSpec"] == "C:\\Windows\\System32\\cmd.exe"
    assert "PATHEXT" not in forwarded
    assert "WINDIR" not in forwarded


def test_build_runtime_config_trims_default_workspace_and_repo_from_env() -> None:
    """Whitespace-padded env values must be stripped, and a pure-whitespace
    value must become None (not the literal whitespace)."""
    env = {
        "ENGRAPHIS_WORKSPACE": "   ",
        "ENGRAPHIS_REPO": "\trepo\t",
        "ENGRAPHIS_DB_PATH": "  /tmp/x.db  ",
    }
    cfg = build_runtime_config(env=env)
    assert cfg.default_workspace is None
    assert cfg.default_repo == "repo"
    # The env allowlist also strips; the entry must reflect the trimmed value.
    assert cfg.environment["ENGRAPHIS_DB_PATH"] == "/tmp/x.db"


def test_build_runtime_config_does_not_mutate_input_env() -> None:
    """`build_runtime_config` must not mutate the caller's env mapping."""
    env = {
        "ENGRAPHIS_WORKSPACE": "  ws  ",
        "ENGRAPHIS_REPO": "  repo  ",
        "ENGRAPHIS_DB_PATH": "  /tmp/x.db  ",
        "PATH": "  /usr/bin  ",
    }
    snapshot = dict(env)
    build_runtime_config(env=env)
    assert env == snapshot


def test_engraphis_environment_empty_input_returns_empty_dict() -> None:
    """Defensive: an empty mapping must produce an empty dict, not raise."""
    assert _engraphis_environment({}) == {}


def test_engraphis_environment_skips_prefix_only_keys_without_value() -> None:
    """An ENGRAPHIS_-prefixed key whose value is non-string must be skipped
    rather than forwarded as-is (which would crash subprocess.Popen)."""
    env = {
        "ENGRAPHIS_DB_PATH": 42,  # type: ignore[dict-item]
        "ENGRAPHIS_WORKSPACE": None,  # type: ignore[dict-item]
    }
    assert _engraphis_environment(env) == {}  # type: ignore[arg-type]


def test_non_blank_strips_tabs_and_newlines() -> None:
    """`_non_blank` is the single source of truth for trimming env values;
    tabs and newlines should be treated like spaces."""
    assert _non_blank("\t\n  hi  \n\t") == "hi"
    assert _non_blank("\t\n  \n\t") is None


def test_runtime_config_as_subprocess_env_returns_independent_copy() -> None:
    """Mutating the dict returned by as_subprocess_env must not change the
    frozen config's own mapping."""
    cfg = EngraphisRuntimeConfig(
        command="x",
        environment={"ENGRAPHIS_DB_PATH": "/tmp/x.db"},
    )
    env = cfg.as_subprocess_env()
    env["ENGRAPHIS_DB_PATH"] = "/mutated/y.db"
    assert cfg.environment["ENGRAPHIS_DB_PATH"] == "/tmp/x.db"
