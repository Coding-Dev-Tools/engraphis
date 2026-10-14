"""Host-facing policy for protecting files from coding-agent writes.

Engraphis does not mediate an agent's filesystem access.  This module therefore
provides a small, dependency-free decision point for hosts that *do* mediate
tool calls.  Policies live outside the repository by default so the agent being
constrained cannot weaken its own policy with an ordinary repository edit.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Optional, Sequence


POLICY_VERSION = 1
DEFAULT_POLICY_PATH = Path.home() / ".engraphis" / "file-protection.json"
SHELL_TOOLS = frozenset({"bash", "shell", "terminal", "execute", "runcommand"})
FILE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})


class FileProtectionError(ValueError):
    """The operator-owned protection policy is absent, unsafe, or malformed."""


@dataclass(frozen=True)
class ProtectionRule:
    root: Path
    patterns: tuple[str, ...]
    allow_shell: bool = False


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    matched_pattern: Optional[str] = None


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def _validate_policy_file(path: Path) -> None:
    """Require an owner-controlled regular file on POSIX.

    Windows ACLs do not map to POSIX mode bits, so Windows hosts must enforce
    ownership when installing the hook.  Symlinks are rejected everywhere.
    """
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise FileProtectionError(f"file-protection policy not found: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise FileProtectionError("file-protection policy must be a regular file")
    if os.name == "posix":
        if info.st_uid != os.geteuid():
            raise FileProtectionError("file-protection policy must be owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise FileProtectionError(
                "file-protection policy must not be group- or world-writable"
            )


def load_policy(path: Optional[Path] = None) -> tuple[ProtectionRule, ...]:
    """Load and strictly validate an operator-owned JSON policy."""
    policy_path = Path(os.path.abspath(os.path.expanduser(os.fspath(
        path or Path(os.environ.get(
            "ENGRAPHIS_FILE_PROTECTION_POLICY", DEFAULT_POLICY_PATH,
        ))
    ))))
    _validate_policy_file(policy_path)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FileProtectionError(f"cannot read file-protection policy: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != POLICY_VERSION:
        raise FileProtectionError(f"file-protection policy version must be {POLICY_VERSION}")
    raw_rules = payload.get("roots")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise FileProtectionError("file-protection policy requires a non-empty roots list")
    rules = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise FileProtectionError("each file-protection root must be an object")
        root_value = raw.get("path")
        patterns = raw.get("protect")
        if not isinstance(root_value, str) or not Path(root_value).expanduser().is_absolute():
            raise FileProtectionError("each protected root path must be absolute")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(item, str) and item and not Path(item).is_absolute()
            for item in patterns
        ):
            raise FileProtectionError("protect must contain non-empty relative patterns")
        normalized_patterns = []
        for pattern in patterns:
            normalized = pattern.replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized in {"", ".", ".."} or normalized.startswith("../"):
                raise FileProtectionError("protected patterns may not escape their root")
            normalized_patterns.append(normalized)
        rules.append(ProtectionRule(
            root=_canonical(Path(root_value)),
            patterns=tuple(normalized_patterns),
            allow_shell=raw.get("allow_shell", False) is True,
        ))
    # A direct file-edit tool must not be able to rewrite the policy that will
    # adjudicate its next call.  Host hook configuration remains a host/OS
    # boundary and should likewise be owner-protected.
    rules.append(ProtectionRule(policy_path.parent, (policy_path.name,), allow_shell=True))
    return tuple(rules)


def _rule_for_path(path: Path, rules: Sequence[ProtectionRule]) -> tuple[Optional[ProtectionRule], Optional[str]]:
    # Check both the named path and its resolved target.  The former protects a
    # protected symlink entry; the latter prevents an unprotected alias from
    # reaching a protected target.
    named = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    candidates = (named, _canonical(named))
    for rule in rules:
        for candidate in candidates:
            try:
                relative = candidate.relative_to(rule.root).as_posix()
            except ValueError:
                continue
            for pattern in rule.patterns:
                # ``fnmatch`` makes ``dir/**`` useful for descendants; the explicit
                # directory comparison also protects the directory entry itself.
                prefix = pattern[:-3].rstrip("/") if pattern.endswith("/**") else None
                if fnmatch.fnmatchcase(relative, pattern) or relative == prefix:
                    return rule, pattern
    return None, None


def decide_tool_call(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    cwd: Path,
    rules: Sequence[ProtectionRule],
) -> GuardDecision:
    """Return whether a host-mediated coding tool call may proceed.

    General shell tools are denied for a protected root by default because
    command-string inspection cannot prove that redirections, subprocesses, or
    scripts will leave protected paths untouched.  An operator can explicitly
    opt out with ``allow_shell`` after providing a stronger OS sandbox.
    """
    normalized_tool = str(tool_name or "").replace("_", "").casefold()
    current = _canonical(cwd)
    if normalized_tool in SHELL_TOOLS and any(not rule.allow_shell for rule in rules):
        return GuardDecision(False, "shell tools are disabled while file protection is active")
    if normalized_tool not in FILE_TOOLS:
        return GuardDecision(True, "tool does not expose a recognized direct file write")
    values = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    if not values:
        return GuardDecision(False, "file-writing tool did not provide a target path")
    for value in values:
        target = Path(value)
        if not target.is_absolute():
            target = current / target
        _, pattern = _rule_for_path(target, rules)
        if pattern is not None:
            return GuardDecision(False, "target matches an operator-protected path", pattern)
    return GuardDecision(True, "targets do not match protected paths")
