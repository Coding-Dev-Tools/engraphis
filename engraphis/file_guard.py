"""Host-facing policy for protecting files from coding-agent writes.

Engraphis does not mediate an agent's filesystem access.  This module therefore
provides a small, dependency-free decision point for hosts that *do* mediate
tool calls.  Policies live outside the repository by default so the agent being
constrained cannot weaken its own policy with an ordinary repository edit.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import functools
import json
import os
from os.path import normcase as _normcase
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Optional, Sequence
import unicodedata


POLICY_VERSION = 1
DEFAULT_POLICY_PATH = Path.home() / ".engraphis" / "file-protection.json"
# Names are compared after removing separators and case-folding.  Command Code
# sends canonical ids (``shell_command``, ``write_file``, ``edit_file``) as
# ``tool_name``; the other entries cover Claude-Code-style hosts.
SHELL_TOOLS = frozenset({
    "bash", "shell", "terminal", "execute", "exec", "execcommand", "runcommand",
    "shellcommand", "sh", "zsh", "cmd", "powershell", "pwsh",
    "consoleterminal", "runterminalcmd",
})
# Every id below performs a *direct* filesystem mutation.  A host tool that is
# missing here is allowed by the evaluator, so the list has to cover the
# mutating names the supported hosts actually send -- including the
# Claude-Code-style ``ApplyPatch`` and the file create/delete/move ids.
FILE_TOOLS = frozenset({
    "write", "edit", "multiedit", "notebookedit", "applypatch",
    "writefile", "editfile", "createfile", "deletefile", "movefile",
    "renamefile", "copyfile", "updatefile", "writetofile", "replacefile",
    "strreplaceeditor",
})
# Separators are dropped before comparing a tool id, so ``write_file``,
# ``write-file`` and ``Write`` name one tool while a near miss such as
# ``write_filex`` or ``rewrite`` stays a different tool.
_TOOL_NAME_SEPARATORS = re.compile(r"[^0-9a-z]+")


# Win32 accepts path aliases this evaluator cannot map back to a protected root.
_WINDOWS = os.name == "nt"
# POSIX path semantics are case-sensitive by default; only the volume can say
# otherwise (macOS ships case-insensitive volumes).
_POSIX = os.name == "posix"


def _unverifiable_windows_target(value: str) -> bool:
    r"""Return True for a Win32 target this evaluator cannot map to a protected root.

    Device and extended-length prefixes (``\\?\``, ``\\.\``), UNC and admin shares
    (``\\localhost\C$\...``), and NTFS alternate data streams (``.env::$DATA`` writes
    ``.env``) all name files without matching the root-relative patterns, so writes
    through them are denied while protection is active.  A drive-relative path
    (``C:notes.txt``) is denied for the same reason: it resolves against the
    current directory *of that drive*, which this evaluator cannot map back to a
    protected root.
    """
    text = value.lstrip().replace("/", "\\")
    if text.startswith("\\\\"):
        return True
    has_drive = len(text) >= 2 and text[1] == ":" and text[0].isascii() and text[0].isalpha()
    if has_drive:
        rest = text[2:]
        if not rest.startswith("\\"):
            # Drive-relative: "C:notes.txt" is relative to the current
            # directory of drive C:, not to the process working directory.
            return True
        return ":" in rest
    return ":" in text


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


def _normalize_tool_name(tool_name: Any) -> str:
    """Reduce a host tool id to a comparable key."""
    return _TOOL_NAME_SEPARATORS.sub("", str(tool_name or "").casefold())


@functools.lru_cache(maxsize=64)
def _filesystem_is_case_insensitive(root: str) -> bool:
    """Return True when the filesystem holding ``root`` ignores case.

    macOS volumes are case-insensitive by default, so a case-sensitive POSIX
    comparison would let ``.ENV`` overwrite a protected ``.env``.  The
    filesystem itself is asked instead of trusting the platform name.
    """
    probe = Path(root)
    for _ in range(32):
        parent = probe.parent
        name = probe.name
        if not name:
            return False
        variant = name.swapcase()
        if variant != name and os.path.lexists(str(parent / variant)):
            return True
        try:
            entries = os.listdir(parent)
        except OSError:
            entries = []
        for entry in entries:
            swapped = entry.swapcase()
            if swapped != entry and os.path.lexists(str(parent / swapped)):
                return True
        if parent == probe:
            return False
        probe = parent
    return False


def _ignore_case(root: Path) -> bool:
    """Return True when path comparison must ignore case below ``root``."""
    if _normcase("A") != "A":
        # Windows-style normcase already folds case.
        return True
    if not _POSIX:
        return False
    return _filesystem_is_case_insensitive(str(root))


def _strip_windows_trailing(text: str) -> str:
    """Drop the trailing dots and spaces Win32 drops from path components.

    ``.env `` and ``.env.`` name the protected ``.env`` file on Windows, so the
    comparison has to see the same name the operating system will use.
    """
    if not _WINDOWS:
        return text
    cleaned = []
    for part in text.split("/"):
        if part in {".", ".."}:
            cleaned.append(part)
        else:
            cleaned.append(part.rstrip(" ."))
    return "/".join(cleaned)


def _comparison_key(text: str, *, ignore_case: bool) -> str:
    """Normalize a root-relative path or a policy pattern for comparison."""
    value = unicodedata.normalize("NFC", text)
    value = _strip_windows_trailing(value)
    value = _normcase(value)
    if ignore_case:
        value = value.casefold()
    return value


def _relative_parts(candidate: Path, root: Path, *, ignore_case: bool) -> Optional[str]:
    """Return ``candidate`` relative to ``root`` as a POSIX string, or None.

    ``Path.relative_to`` compares case-sensitively on POSIX, which would miss a
    protected path on a case-insensitive volume, so the comparison is done
    component-wise here.
    """
    candidate_parts = tuple(candidate.parts)
    root_parts = tuple(root.parts)
    if len(candidate_parts) < len(root_parts):
        return None
    head = candidate_parts[:len(root_parts)]
    if ignore_case:
        matched = all(
            left.casefold() == right.casefold()
            for left, right in zip(head, root_parts)
        )
    else:
        matched = head == root_parts
    if not matched:
        return None
    return "/".join(candidate_parts[len(root_parts):])


def _is_absolute_pattern(pattern: str) -> bool:
    """Return True for a pattern that names an absolute path on any platform.

    The loader has to reject these platform-independently: a POSIX-style
    ``/etc/passwd`` is not absolute on Windows, and ``C:\\x`` is not absolute on
    POSIX, so a platform-local check would silently accept a pattern that can
    never match a root-relative path.
    """
    if pattern.startswith("/") or pattern.startswith("\\"):
        return True
    if len(pattern) >= 2 and pattern[1] == ":" and pattern[0].isascii() and pattern[0].isalpha():
        return True
    return False


def _normalize_pattern(pattern: str) -> str:
    """Normalize one protected pattern, refusing anything that cannot match.

    A pattern that names an absolute path or climbs out of the root can never
    match a root-relative target, so accepting it would silently leave the
    operator's rule inert.
    """
    normalized = pattern.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise FileProtectionError("protected patterns may not escape their root")
    if any(part == ".." for part in normalized.split("/")):
        raise FileProtectionError("protected patterns may not escape their root")
    if _is_absolute_pattern(normalized):
        raise FileProtectionError("protected patterns may not be absolute paths")
    if normalized.startswith("~"):
        raise FileProtectionError("protected patterns must be relative to their root")
    return normalized


def _check_policy_ownership(info: os.stat_result) -> None:
    """Require an owner-controlled policy file on POSIX.

    Windows ACLs do not map to POSIX mode bits, so Windows hosts must enforce
    ownership with NTFS permissions on the policy directory.
    """
    if os.name == "posix":
        if info.st_uid != os.geteuid():
            raise FileProtectionError("file-protection policy must be owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise FileProtectionError(
                "file-protection policy must not be group- or world-writable"
            )


def _read_policy_text(path: Path) -> str:
    """Validate and read the policy file without a check-then-use race.

    The file is opened once, and the identity of the descriptor that is read is
    compared with the identity that was validated, so a policy file swapped for
    a symlink or another file between the two steps is refused instead of
    honoured.
    """
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise FileProtectionError(f"file-protection policy not found: {path}") from exc
    except OSError as exc:
        raise FileProtectionError(f"cannot read file-protection policy: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise FileProtectionError("file-protection policy must be a regular file")
    _check_policy_ownership(info)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise FileProtectionError(f"cannot read file-protection policy: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise FileProtectionError(
                "file-protection policy changed while it was being read"
            )
        if not stat.S_ISREG(opened.st_mode):
            raise FileProtectionError("file-protection policy must be a regular file")
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        data = handle.read()
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise FileProtectionError(f"cannot read file-protection policy: {exc}") from exc


def _load_policy_payload(text: str) -> Any:
    """Decode the policy document, reporting every failure as one error type."""
    try:
        return json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise FileProtectionError(f"cannot read file-protection policy: {exc}") from exc


def _host_settings_path() -> Path:
    """Return the host hook-configuration file the guard must protect."""
    configured = os.environ.get("COMMANDCODE_SETTINGS_PATH")
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / ".commandcode" / "settings.json"


def load_policy(path: Optional[Path] = None) -> tuple[ProtectionRule, ...]:
    """Load and strictly validate an operator-owned JSON policy."""
    policy_path = Path(os.path.abspath(os.path.expanduser(os.fspath(
        path or Path(os.environ.get(
            "ENGRAPHIS_FILE_PROTECTION_POLICY", DEFAULT_POLICY_PATH,
        ))
    ))))
    payload = _load_policy_payload(_read_policy_text(policy_path))
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
            isinstance(item, str) and item for item in patterns
        ):
            raise FileProtectionError("protect must contain non-empty relative patterns")
        normalized_patterns = []
        for pattern in patterns:
            normalized_patterns.append(_normalize_pattern(pattern))
        rules.append(ProtectionRule(
            root=_canonical(Path(root_value)),
            patterns=tuple(normalized_patterns),
            allow_shell=raw.get("allow_shell", False) is True,
        ))
    # A direct file-edit tool must not be able to rewrite the policy that will
    # adjudicate its next call, nor the host configuration that decides which
    # tool calls reach this guard in the first place.  Host hook configuration
    # remains a host/OS boundary and should likewise be owner-protected.
    settings_path = _host_settings_path()
    for protected_parent, protected_name in (
        (policy_path.parent, policy_path.name),
        (settings_path.parent, settings_path.name),
    ):
        rules.append(ProtectionRule(
            _canonical(protected_parent), (protected_name,), allow_shell=True,
        ))
    return tuple(rules)


def _rule_for_path(path: Path, rules: Sequence[ProtectionRule]) -> tuple[Optional[ProtectionRule], Optional[str]]:
    # Check both the named path and its resolved target.  The former protects a
    # protected symlink entry; the latter prevents an unprotected alias from
    # reaching a protected target.
    named = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    # Resolve the *unnormalized* path: on POSIX the kernel follows a symlink
    # before applying a later ``..``, so ``link/../x`` must not be collapsed
    # lexically first or it can name a protected target as an unprotected one.
    candidates = (named, _canonical(path))
    for rule in rules:
        ignore_case = _ignore_case(rule.root)
        for candidate in candidates:
            relative = _relative_parts(candidate, rule.root, ignore_case=ignore_case)
            if relative is None:
                continue
            key = _comparison_key(relative, ignore_case=ignore_case)
            for pattern in rule.patterns:
                # ``fnmatch`` makes ``dir/**`` useful for descendants; the explicit
                # directory comparison also protects the directory entry itself.
                # realpath cannot correct the casing of a target that does not exist yet.
                normalized_pattern = _comparison_key(pattern, ignore_case=ignore_case)
                prefix = (
                    _comparison_key(pattern[:-3].rstrip("/"), ignore_case=ignore_case)
                    if pattern.endswith("/**") else None
                )
                if fnmatch.fnmatchcase(key, normalized_pattern) or key == prefix:
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
    normalized_tool = _normalize_tool_name(tool_name)
    if normalized_tool in SHELL_TOOLS and any(not rule.allow_shell for rule in rules):
        return GuardDecision(False, "shell tools are disabled while file protection is active")
    if normalized_tool not in FILE_TOOLS:
        return GuardDecision(True, "tool does not expose a recognized direct file write")
    if _WINDOWS and _unverifiable_windows_target(os.fspath(cwd)):
        return GuardDecision(
            False,
            "Windows device, UNC, drive-relative, or alternate-data-stream "
            "targets cannot be verified",
        )
    current = _canonical(cwd)
    values = []
    for key in ("file_path", "filePath", "path", "notebook_path", "notebookPath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    if not values:
        return GuardDecision(False, "file-writing tool did not provide a target path")
    for value in values:
        if _WINDOWS and _unverifiable_windows_target(value):
            return GuardDecision(
                False,
                "Windows device, UNC, drive-relative, or alternate-data-stream "
                "targets cannot be verified",
            )
        target = Path(value)
        if not target.is_absolute():
            target = current / target
        _, pattern = _rule_for_path(target, rules)
        if pattern is not None:
            return GuardDecision(False, "target matches an operator-protected path", pattern)
    return GuardDecision(True, "targets do not match protected paths")
