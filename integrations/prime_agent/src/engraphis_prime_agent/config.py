"""Runtime configuration for the engraphis-mcp stdio gateway.

Mirrors integrations/pi/src/config.ts: a bounded environment allowlist, an
overridable console command, and explicit default workspace/repo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

EXTENSION_VERSION = "0.1.0"

CORE_DIRECT_TOOLS: tuple[str, ...] = (
    "engraphis_session",
    "engraphis_recall_context",
    "engraphis_remember",
    "engraphis_discover_actions",
    "engraphis_execute_read",
    "engraphis_execute_action",
    "engraphis_get_memory",
    "engraphis_update_memory",
    "engraphis_conflict_review",
)

# 8 sub-agent names. Overridable via PrimeAgentFleet(agent_names=...).
# Invariants enforced at import time: exactly 8 entries, each a non-empty
# string, and all distinct so they can be used as fleet/dict keys.
DEFAULT_AGENT_NAMES: tuple[str, ...] = (
    "researcher",   # gather context, recall prior decisions
    "planner",      # decompose goals into ordered steps
    "coder",        # implement changes
    "reviewer",     # critique diffs and surface risks
    "tester",       # write/run/verify tests
    "documenter",   # capture decisions for durable memory
    "monitor",      # watch logs, regressions, health
    "integrator",   # merge, deploy, coordinate handoffs
)

assert len(DEFAULT_AGENT_NAMES) == 8, "DEFAULT_AGENT_NAMES must contain exactly 8 sub-agents"
assert all(isinstance(n, str) and n for n in DEFAULT_AGENT_NAMES), (
    "DEFAULT_AGENT_NAMES entries must be non-empty strings"
)
assert len(set(DEFAULT_AGENT_NAMES)) == len(DEFAULT_AGENT_NAMES), (
    "DEFAULT_AGENT_NAMES entries must be unique"
)

# Allowlist, identical to integrations/pi/src/config.ts::engraphisEnvironment.
#
# Note on case sensitivity:
#   * POSIX is case-sensitive: only ``PATH`` exists; ``Path`` would be a
#     separate variable and is harmless to include.
#   * Windows is case-insensitive: ``PATH``, ``Path``, and ``path`` all refer
#     to the same environment entry. Including both ``PATH`` and ``Path`` is
#     redundant on Windows but never harmful — the OS lookups normalise case
#     and Python's ``os.environ`` preserves the case of the *first* writer.
#     We keep both for symmetry with the Pi TS implementation.
_ALLOWED_ENV_KEYS = frozenset({
    "PATH", "Path", "SystemRoot", "ComSpec",
    # Windows-only home variables. ``Path.home()`` reads USERPROFILE first
    # and falls back to HOMEDRIVE+HOMEPATH; without them the early
    # ``_resolve_config_env_path()`` call aborts with FileNotFoundError on
    # ``~/.engraphis.env`` before the MCP handshake runs. Forward them on
    # every platform so a wheel installed on Windows does not need a
    # pre-existing ``ENGRAPHIS_ENV_FILE`` to bootstrap.
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
})
_ALLOWED_ENV_PREFIX = "ENGRAPHIS_"


@dataclass(frozen=True)
class EngraphisRuntimeConfig:
    """Resolved runtime configuration for the stdio gateway subprocess.

    The dataclass is frozen: attributes cannot be reassigned after ``__init__``.
    The mutable-looking fields (``args``, ``environment``) are normalised in
    :meth:`__post_init__` so that callers cannot mutate them in place either
    — ``args`` becomes a ``tuple`` and ``environment`` is a shallow copy of
    the input mapping stored as an immutable-style ``dict[str, str]``.

    :param command: Executable name or absolute path of the MCP gateway
        binary. Must be a non-empty string; falls back to ``"engraphis-mcp"``
        on the PATH when constructed via :func:`build_runtime_config`.
    :param args: Positional arguments passed to ``command``. Frozen as a
        tuple at construction time.
    :param cwd: Optional working directory for the subprocess. The value
        is forwarded unchanged to the runtime layer, which is responsible
        for path resolution and existence checks; this class only enforces
        that, when provided, it is a non-empty string.
    :param default_workspace: Optional default workspace identifier
        forwarded to the gateway (typically a memory scope key).
    :param default_repo: Optional default repository identifier forwarded
        to the gateway.
    :param environment: Allowlist-filtered environment variables to pass
        to the subprocess. Stored as a defensive copy.
    """

    command: str = "engraphis-mcp"
    args: tuple[str, ...] = ()
    cwd: str | None = None
    default_workspace: str | None = None
    default_repo: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate `command`: must be a non-empty string. We check truthiness
        # after stripping so a bare-whitespace value is rejected too.
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("EngraphisRuntimeConfig.command must be a non-empty string")
        # Normalise `command` in place (frozen dataclass requires object.__setattr__).
        object.__setattr__(self, "command", self.command.strip())

        # Freeze `args` as a tuple. Accept any iterable of strings; reject
        # non-string entries to surface caller mistakes early.
        normalised_args: tuple[str, ...] = tuple(self.args)
        for a in normalised_args:
            if not isinstance(a, str):
                raise TypeError(
                    f"EngraphisRuntimeConfig.args entries must be str, got {type(a).__name__}"
                )
        object.__setattr__(self, "args", normalised_args)

        # `cwd`: light validation. The runtime layer is responsible for
        # path resolution and existence checks; here we only ensure that,
        # when provided, the value is a non-empty string. Relative paths
        # are allowed and resolved relative to the parent process cwd.
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd):
            raise ValueError("EngraphisRuntimeConfig.cwd must be a non-empty string or None")

        # Defensive copy of the environment mapping. We also coerce values
        # to str to give the field a precise ``Mapping[str, str]`` shape
        # even if a caller passed a more permissive type.
        env_copy: dict[str, str] = {str(k): str(v) for k, v in dict(self.environment).items()}
        object.__setattr__(self, "environment", env_copy)

    def as_subprocess_env(self) -> dict[str, str]:
        """Return a fresh ``dict`` copy of the environment for subprocess use.

        Always returns a new mapping so callers can mutate the result
        without affecting this config's frozen state.
        """
        return dict(self.environment)


def _non_blank(value: str | None) -> str | None:
    """Return ``value`` with surrounding whitespace stripped, or ``None``.

    A value that is ``None``, empty, or whitespace-only returns ``None``;
    otherwise the stripped string is returned. Used to normalise optional
    environment overrides before they are stored on the config.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _engraphis_environment(env: Mapping[str, Any]) -> dict[str, str]:
    """Forward only the Engraphis settings and the Windows/POSIX path vars.

    Mirrors integrations/pi/src/config.ts so a sub-agent's gateway sees the
    same allowlist the Pi extension uses.

    The parameter is typed ``Mapping[str, Any]`` because real-world
    sources (``os.environ`` is fine, but test fixtures and ad-hoc dicts may
    contain ``None`` or other non-string values). Non-string values are
    silently dropped — this is intentional: a missing or wrongly-typed
    variable should not crash config construction, it should just be
    excluded from the forwarded environment.
    """
    forwarded: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(value, str):
            continue
        if key.startswith(_ALLOWED_ENV_PREFIX) or key in _ALLOWED_ENV_KEYS:
            # Trim surrounding whitespace so a value like "  /tmp/x.db  " is
            # forwarded as "/tmp/x.db". This keeps gateway config (paths,
            # workspace ids, repo names) free of accidental padding and
            # matches the trimming `_non_blank` applies to the dedicated
            # workspace/repo fields.
            forwarded[key] = value.strip()
    return forwarded


def build_runtime_config(
    env: Mapping[str, Any] | None = None,
    *,
    command: str | None = None,
    args: tuple[str, ...] | None = None,
    cwd: str | None = None,
) -> EngraphisRuntimeConfig:
    """Build the runtime config the same way the Pi TS integration does.

    Reads from ``env`` (defaults to :data:`os.environ`) with the following
    resolution order for each field:

    * ``command`` — explicit ``command`` kwarg, else
      ``$ENGRAPHIS_MCP_COMMAND``, else ``"engraphis-mcp"``.
    * ``args`` — explicit ``args`` kwarg, else ``()``.
    * ``cwd`` — explicit ``cwd`` kwarg, else ``None``.
    * ``default_workspace`` — ``$ENGRAPHIS_WORKSPACE`` (trimmed;
      whitespace-only becomes ``None``).
    * ``default_repo`` — ``$ENGRAPHIS_REPO`` (trimmed).
    * ``environment`` — allowlist-filtered view of ``env``; only keys with
      the ``ENGRAPHIS_`` prefix or in :data:`_ALLOWED_ENV_KEYS` are
      forwarded, and only when their value is a ``str``.

    The returned :class:`EngraphisRuntimeConfig` is frozen and stores
    defensive copies of any mutable inputs.
    """
    src: Mapping[str, Any] = os.environ if env is None else env
    resolved_command = (
        _non_blank(command)
        or _non_blank(src.get("ENGRAPHIS_MCP_COMMAND"))  # type: ignore[arg-type]
        or "engraphis-mcp"
    )
    forwarded = _engraphis_environment(src)
    workspace = _non_blank(src.get("ENGRAPHIS_WORKSPACE"))  # type: ignore[arg-type]
    repo = _non_blank(src.get("ENGRAPHIS_REPO"))  # type: ignore[arg-type]
    return EngraphisRuntimeConfig(
        command=resolved_command,
        args=tuple(args or ()),
        cwd=cwd,
        default_workspace=workspace,
        default_repo=repo,
        environment=forwarded,
    )
