"""Zero-friction passive capture, bounded turn-0 briefings, and client hook management.

Dependency-free core (stdlib only; no FastAPI, MCP, or PyTorch imports at module load)
compatible with Python 3.9+. Designed to pair with :mod:`engraphis.hooks_cli` so coding
agents (Claude Code, Cursor, Command Code) get:

1. Bounded turn-0 session briefings citing only ``prompt_eligible`` memories.
2. Deterministic, fail-closed secret-checked passive capture at session stop / commit,
   staged as ``source="passive-hook"`` (``review_state="pending"``) with normalized-hash
   deduplication across active, pending, and quarantined workspace records.
3. Idempotent, backup-first hook installation and uninstallation without touching
   unrelated user hooks.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from engraphis.core.poisoning import prompt_eligible
from engraphis.core.secrets import SecretDetectedError, redact_secrets, reject_secrets

SUPPORTED_CLIENTS = ("claude", "cursor", "commandcode")
BRIEFING_HOOK_NAME = "engraphis-session-briefing"
CAPTURE_HOOK_NAME = "engraphis-passive-capture"
LEGACY_CC_SESSION_KEY = "cc-engraphis-session-start"
_MAX_WORKSPACE_HASH_SCAN = 50_000
_WINDOWS_SHELL_META_RE = re.compile(r'[\r\n\x00%&|<>^]')
_POSIX_BAD_ARG_RE = re.compile(r'[\r\n\x00]')

# Deterministic signal markers identifying durable engineering knowledge rather than
# transient debug chatter.
_SIGNAL_PATTERNS = (
    re.compile(r"\b(?:decided|decision|architecture|architectural|invariant|convention)\b", re.I),
    re.compile(r"\b(?:never|always|must not|must always|do not|required to)\b", re.I),
    re.compile(r"\b(?:root cause|caused by|fixed by|fix:|workaround|gotcha|migration)\b", re.I),
    re.compile(r"\b(?:supersedes|replaced|deprecated|migrated from|switched from)\b", re.I),
    re.compile(r"\b(?:constraint|policy|security|rollback|runbook|adr)\b", re.I),
)
_NOISE_PREFIXES = (
    "hi ", "hello ", "thanks", "thank you", "sure,", "okay", "ok,", "got it",
    "let me ", "i will ", "i'll ", "running ", "checking ", "looking at ",
    "reading ", "editing ", "searching ", "```", "{", "[",
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MIN_CANDIDATE_CHARS = 28
_MAX_CANDIDATE_CHARS = 600


def normalize_candidate_text(text: str) -> str:
    """Strip non-whitespace ASCII control chars and collapse whitespace stably."""
    cleaned = _CONTROL_RE.sub("", str(text or ""))
    return " ".join(cleaned.split()).strip()


def candidate_content_hash(text: str) -> str:
    """Return a deterministic SHA-256 hex digest of normalized case-folded text."""
    norm = normalize_candidate_text(text).casefold()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def extract_durable_candidates(text: str, *, max_candidates: int = 5) -> list[str]:
    """Extract bounded, high-signal durable engineering statements from raw prose.

    Deterministic and offline (no LLM calls). Splits on newlines/bullets while skipping
    fenced code blocks, short chatter, and intra-batch duplicates.
    """
    if not text or max_candidates < 1:
        return []
    candidates: list[str] = []
    seen_hashes: set[str] = set()
    in_fence = False

    for raw_line in str(text).splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        # Strip leading markdown list/quote prefixes cleanly.
        cleaned = re.sub(r"^(?:[-*+•]\s+|\d+[.)]\s+|>\s+)", "", stripped).strip()
        cleaned = normalize_candidate_text(cleaned)
        if len(cleaned) < _MIN_CANDIDATE_CHARS or len(cleaned) > _MAX_CANDIDATE_CHARS:
            continue
        low = cleaned.casefold()
        if any(low.startswith(prefix) for prefix in _NOISE_PREFIXES):
            continue
        if not any(rx.search(cleaned) for rx in _SIGNAL_PATTERNS):
            continue
        digest = candidate_content_hash(cleaned)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        candidates.append(cleaned)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _read_jsonl_assistant_tail(path: Path, *, max_messages: int = 6, max_bytes: int = 65536) -> str:
    """Safely read a bounded tail of a regular non-symlink JSONL transcript."""
    if path.is_symlink():
        return ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return ""
        size = st.st_size
        skipped_head = size > max_bytes
        if skipped_head:
            os.lseek(fd, size - max_bytes, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            block = os.read(fd, min(remaining, 16384))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw_bytes = b"".join(chunks)
    except OSError:
        return ""
    finally:
        os.close(fd)
    if skipped_head:
        _, _, raw_bytes = raw_bytes.partition(b"\n")
    raw = raw_bytes.decode("utf-8", errors="replace")

    messages: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        # Claude Code JSONL wraps assistant turns as {"type": "assistant", "message": {"role": "assistant", "content": [...]}}.
        if obj.get("content") is None and isinstance(obj.get("message"), dict):
            obj = obj["message"]
        role = str(obj.get("role") or obj.get("type") or "").casefold()
        if role not in {"assistant", "model", "summary"}:
            continue
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            messages.append(content.strip())
        elif isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            ]
            joined = "\n".join(p for p in parts if p.strip())
            if joined:
                messages.append(joined)
    return "\n".join(messages[-max_messages:])


def extract_text_from_hook_payload(raw_stdin: str) -> tuple[str, dict[str, Any]]:
    """Parse Claude Code / Cursor Stop-hook JSON stdin or fall back to plain text.

    Returns ``(extracted_text, meta)``. If ``stop_hook_active`` is True in a JSON hook
    payload, returns empty text with ``meta["skipped_stop_loop"] = True`` to prevent
    recursive hook loops.
    """
    raw = str(raw_stdin or "").strip()
    if not raw:
        return "", {"format": "empty"}
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except ValueError:
            return raw, {"format": "text"}
        if isinstance(payload, dict):
            cwd_val = payload.get("cwd") or ""
            if not cwd_val and isinstance(payload.get("workspace_roots"), list):
                for root in payload["workspace_roots"]:
                    if isinstance(root, str) and root.strip():
                        cwd_val = root.strip()
                        break
            meta: dict[str, Any] = {
                "format": "json",
                "cwd": cwd_val,
                "hook_event_name": payload.get("hook_event_name") or "",
            }
            if payload.get("stop_hook_active") is True:
                meta["skipped_stop_loop"] = True
                return "", meta
            chunks: list[str] = []
            for key in ("last_assistant_message", "summary", "text", "content", "message"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    chunks.append(val.strip())
            tpath = payload.get("transcript_path")
            if not chunks and isinstance(tpath, str) and tpath.strip():
                tail = _read_jsonl_assistant_tail(Path(tpath).expanduser())
                if tail:
                    chunks.append(tail)
            return "\n".join(chunks), meta
    return raw, {"format": "text"}


def format_briefing_hook_output(
    context_text: str, *, client: str = "claude"
) -> dict[str, Any]:
    """Format a turn-0 briefing payload for the target coding-agent hook contract."""
    if not context_text:
        return {}
    if client == "cursor":
        return {"additional_context": context_text}
    return {
        "suppressOutput": False,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_text,
        },
    }


def build_session_briefing(
    service: Any,
    *,
    workspace: str,
    repo: Optional[str] = None,
    query: str = "",
    max_chars: int = 1200,
    k: int = 8,
    client: str = "claude",
) -> dict[str, Any]:
    """Build a bounded, read-only turn-0 context briefing from prompt-eligible memories.

    Never invokes ``MemoryService.recall()`` with an empty query; routes empty queries
    through ``recall_proactive()`` and catches missing-scope ``ValidationError`` cleanly.
    """
    from engraphis.service import ValidationError

    bounded_chars = int(max_chars)
    if bounded_chars <= 0:
        return {
            "workspace": workspace,
            "repo": repo,
            "count": 0,
            "cited_ids": [],
            "context": "",
            "hook_output": format_briefing_hook_output("", client=client),
        }
    cleaned_query = str(query or "").strip()
    memories: list[dict[str, Any]] = []

    try:
        if cleaned_query:
            try:
                res = service.recall(
                    cleaned_query,
                    workspace=workspace,
                    repo=repo,
                    k=k,
                    reinforce=False,
                    record_receipt=False,
                    include_untrusted=False,
                )
                memories = list(res.get("memories") or [])
            except ValidationError:
                memories = []
            if not memories and repo:
                res = service.recall(
                    cleaned_query,
                    workspace=workspace,
                    repo=None,
                    k=k,
                    reinforce=False,
                    record_receipt=False,
                    include_untrusted=False,
                )
                memories = list(res.get("memories") or [])
        else:
            try:
                res = service.recall_proactive(workspace=workspace, repo=repo, k=k)
                memories = list(res.get("memories") or [])
            except ValidationError:
                if repo:
                    res = service.recall_proactive(workspace=workspace, repo=None, k=k)
                    memories = list(res.get("memories") or [])
                else:
                    memories = []
    except ValidationError:
        memories = []

    eligible: list[dict[str, Any]] = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        prov = item.get("provenance")
        meta = item.get("metadata")
        if not prompt_eligible(prov, meta):
            continue
        if item.get("prompt_eligible") is False:
            continue
        eligible.append(item)

    scope_label = f"{workspace}/{repo}" if repo else workspace
    full_header = f"Engraphis durable memory ({scope_label}):\n"
    header = full_header if len(full_header) + 12 <= bounded_chars else ""
    lines: list[str] = []
    used = len(header)
    cited_ids: list[str] = []

    for item in eligible:
        mem_id = str(item.get("id") or "").strip()
        body = normalize_candidate_text(str(item.get("content") or ""))
        if not body:
            continue
        tag = f"[{mem_id}] " if mem_id else ""
        entry = f"- {tag}{body}"
        remaining = bounded_chars - used - (1 if lines else 0)
        if remaining < 8:
            break
        if len(entry) > remaining:
            entry = (entry[: max(1, remaining - 3)].rstrip() + "...") if remaining > 3 else entry[:remaining]
        lines.append(entry)
        used += len(entry) + (1 if len(lines) > 1 else 0)
        if mem_id:
            cited_ids.append(mem_id)

    context_text = (header + "\n".join(lines)) if lines else ""
    if len(context_text) > bounded_chars:
        context_text = (
            (context_text[: max(1, bounded_chars - 3)].rstrip() + "...")
            if bounded_chars > 3
            else context_text[:bounded_chars]
        )

    return {
        "workspace": workspace,
        "repo": repo,
        "count": len(lines),
        "cited_ids": cited_ids,
        "context": context_text,
        "hook_output": format_briefing_hook_output(context_text, client=client),
    }


def _existing_workspace_content_hashes(service: Any, workspace: str) -> set[str]:
    """Collect normalized content hashes across active, pending, and quarantined rows."""
    hashes: set[str] = set()
    ws_id = service._lookup_workspace(workspace)
    if not ws_id:
        return hashes
    conn = getattr(getattr(service, "store", None), "conn", None)
    if conn is None:
        return hashes
    cur = conn.execute(
        "SELECT content FROM memories WHERE workspace_id = ? "
        "AND valid_to IS NULL AND expired_at IS NULL "
        "ORDER BY rowid DESC LIMIT ?",
        (ws_id, _MAX_WORKSPACE_HASH_SCAN),
    )
    try:
        for row in cur:
            content = row[0] if isinstance(row, tuple) else row["content"]
            if content:
                hashes.add(candidate_content_hash(str(content)))
    finally:
        cur.close()
    return hashes


def stage_passive_capture(
    service: Any,
    text: str,
    *,
    workspace: str,
    repo: Optional[str] = None,
    secret_mode: str = "reject",
    max_candidates: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Extract, secret-check, deduplicate, and stage passive memory candidates."""
    from engraphis.service import ValidationError

    if secret_mode not in {"reject", "redact"}:
        raise ValueError(f"unsupported secret_mode: {secret_mode!r}")

    candidates = extract_durable_candidates(text, max_candidates=max_candidates)
    if not candidates:
        return {
            "workspace": workspace,
            "repo": repo,
            "extracted": 0,
            "stored_count": 0,
            "stored_ids": [],
            "quarantined_count": 0,
            "would_store_count": 0,
            "skipped_duplicates": 0,
            "skipped_secrets": 0,
            "skipped_errors": 0,
            "review_states": [],
            "review_state": "pending",
            "dry_run": dry_run,
        }
    existing_hashes = _existing_workspace_content_hashes(service, workspace)

    stored_ids: list[str] = []
    review_states: list[str] = []
    would_store: list[str] = []
    quarantined_count = 0
    skipped_duplicates = 0
    blocked_count = 0
    skipped_errors = 0

    for clean_text in candidates:
        try:
            reject_secrets([("content", clean_text)])
        except SecretDetectedError:
            if secret_mode == "reject":
                blocked_count += 1
                continue
            redacted = redact_secrets(clean_text)
            try:
                reject_secrets([("content", redacted)])
            except SecretDetectedError:
                blocked_count += 1
                continue
            clean_text = redacted

        digest = candidate_content_hash(clean_text)
        if digest in existing_hashes:
            skipped_duplicates += 1
            continue

        if dry_run:
            existing_hashes.add(digest)
            would_store.append(clean_text)
            continue

        try:
            res = service.remember(
                clean_text,
                workspace=workspace,
                repo=repo,
                source="passive-hook",
                metadata={"capture_mode": "passive_hook", "content_hash": digest},
            )
        except SecretDetectedError:
            blocked_count += 1
            continue
        except ValidationError:
            skipped_errors += 1
            continue

        existing_hashes.add(digest)
        op = str(res.get("op") or "").strip().lower()
        if op == "noop":
            skipped_duplicates += 1
            continue
        if op == "quarantined" or str(res.get("review_state") or "") == "quarantined":
            quarantined_count += 1
            review_states.append("quarantined")
            continue
        mem_id = str(res.get("id") or "")
        if mem_id:
            stored_ids.append(mem_id)
            state = str(res.get("review_state") or "pending")
            review_states.append(state)

    return {
        "workspace": workspace,
        "repo": repo,
        "extracted": len(candidates),
        "stored_count": len(stored_ids),
        "stored_ids": stored_ids,
        "quarantined_count": quarantined_count,
        "would_store_count": len(would_store),
        "skipped_duplicates": skipped_duplicates,
        "skipped_secrets": blocked_count,
        "skipped_errors": skipped_errors,
        "review_states": review_states,
        "review_state": review_states[0] if len(set(review_states)) == 1 else ("mixed" if review_states else "pending"),
        "dry_run": dry_run,
    }


def _quote_cmd_arg(value: str) -> str:
    """Quote a command argument safely for shell execution across POSIX and Windows."""
    raw = str(value)
    if os.name == "nt":
        if _WINDOWS_SHELL_META_RE.search(raw):
            raise ValueError("hook command path contains unsafe shell metacharacters")
        quoted = subprocess.list2cmdline([raw])
        return quoted if quoted.startswith('"') else f'"{quoted}"'
    if _POSIX_BAD_ARG_RE.search(raw):
        raise ValueError("hook command path contains unsafe control characters")
    return shlex.quote(raw)


def _resolve_hooks_entrypoint(python_exe: str) -> tuple[str, list[str]]:
    """Resolve a CWD-safe hook command that never prepends '' (CWD) to sys.path.

    Prefer an installed ``engraphis-hooks`` console wrapper sibling of ``python_exe``.
    Otherwise invoke ``engraphis/hooks_cli.py`` by its resolved absolute file path
    (unlike ``python -m``, running a script file by path never adds CWD to ``sys.path``).
    """
    exe_path = Path(python_exe)
    wrapper_name = "engraphis-hooks.exe" if os.name == "nt" else "engraphis-hooks"
    wrapper = exe_path.parent / wrapper_name
    try:
        if wrapper.is_file() and not wrapper.is_symlink():
            return str(wrapper), []
    except OSError:
        pass
    hooks_cli_file = Path(__file__).resolve().with_name("hooks_cli.py")
    return python_exe, [str(hooks_cli_file)]


def _build_hook_command(python_exe: str, subcommand: str, client: str) -> str:
    executable, prefix_args = _resolve_hooks_entrypoint(python_exe)
    parts = [_quote_cmd_arg(executable), *(_quote_cmd_arg(arg) for arg in prefix_args)]
    parts.extend([subcommand, "--hook-mode", "--client", client])
    return " ".join(parts)


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")


def _assert_not_symlink(path: Path, action: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"refusing to {action} symlinked hook config: {path}")


def _backup_first_write(settings_path: Path) -> Optional[Path]:
    _assert_not_symlink(settings_path, "modify")
    if not settings_path.exists():
        return None
    backup = settings_path.with_name(f"{settings_path.name}.bak-engraphis-{_utc_stamp()}")
    if backup.is_symlink():
        raise ValueError(f"refusing to write through symlinked backup path: {backup}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(backup), flags, 0o600)
    except FileExistsError:
        return backup
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(settings_path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    return backup


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    _assert_not_symlink(path, "overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    _assert_not_symlink(path, "read")
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig").strip() or "{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return parsed


def _is_engraphis_cursor_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    cmd = str(entry.get("command") or "")
    return (
        "engraphis.hooks_cli" in cmd
        or ("engraphis" in cmd and "hooks_cli.py" in cmd)
        or "engraphis-hooks" in cmd
    )


def _upsert_named_hook_event(
    hooks_dict: dict[str, Any],
    event: str,
    name: str,
    command: str,
    *,
    timeout: int = 10,
) -> None:
    wrappers = hooks_dict.setdefault(event, [])
    if not isinstance(wrappers, list):
        raise ValueError(f"hooks.{event} must be a list")
    new_entry = {"type": "command", "name": name, "command": command, "timeout": timeout}
    for wrapper in wrappers:
        if not isinstance(wrapper, dict):
            continue
        entries = wrapper.get("hooks")
        if not isinstance(entries, list):
            continue
        for idx, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("name") == name:
                entries[idx] = new_entry
                return
    wrappers.append({"hooks": [new_entry]})


def _remove_named_hook_event(hooks_dict: dict[str, Any], event: str, names: Sequence[str]) -> int:
    wrappers = hooks_dict.get(event)
    if not isinstance(wrappers, list):
        return 0
    removed = 0
    kept_wrappers: list[Any] = []
    for wrapper in wrappers:
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("hooks"), list):
            kept_wrappers.append(wrapper)
            continue
        new_entries = []
        for entry in wrapper["hooks"]:
            if isinstance(entry, dict) and entry.get("name") in names:
                removed += 1
            else:
                new_entries.append(entry)
        if new_entries:
            updated = dict(wrapper)
            updated["hooks"] = new_entries
            kept_wrappers.append(updated)
    if kept_wrappers:
        hooks_dict[event] = kept_wrappers
    elif event in hooks_dict:
        del hooks_dict[event]
    return removed


def _has_hook_name(hooks_dict: Mapping[str, Any], event: str, name: str) -> bool:
    wrappers = hooks_dict.get(event)
    if not isinstance(wrappers, list):
        return False
    for wrapper in wrappers:
        if not isinstance(wrapper, dict):
            continue
        for entry in wrapper.get("hooks") or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return True
    return False


def default_client_settings_path(client: str, *, home: Optional[Path] = None, cwd: Optional[Path] = None) -> Path:
    """Resolve the default hook settings file path for a supported client."""
    base_home = home or Path.home()
    base_cwd = cwd or Path.cwd()
    if client == "claude":
        return base_home / ".claude" / "settings.json"
    if client == "commandcode":
        return base_home / ".commandcode" / "settings.json"
    if client == "cursor":
        return base_cwd / ".cursor" / "hooks.json"
    raise ValueError(f"unsupported client: {client!r}; expected one of {SUPPORTED_CLIENTS}")


def install_client_hooks(
    client: str,
    settings_path: Path,
    *,
    python_exe: Optional[str] = None,
    include_stop: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotently install Engraphis SessionStart + Stop hooks for ``client``."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"unsupported client: {client!r}; expected one of {SUPPORTED_CLIENTS}")
    exe = python_exe or sys.executable
    data = _read_json_object(settings_path)
    installed_events: list[str] = []

    if client == "cursor":
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"{settings_path}: 'hooks' must be an object")
        for event, subcmd, enabled in (
            ("sessionStart", "briefing", True),
            ("afterAgentResponse", "capture", include_stop),
        ):
            if not enabled:
                continue
            existing = hooks.get(event)
            if existing is None:
                existing = []
            elif not isinstance(existing, list):
                raise ValueError(f"{settings_path}: hooks.{event} must be a list")
            entries = [e for e in existing if not _is_engraphis_cursor_entry(e)]
            entries.append({"command": _build_hook_command(exe, subcmd, "cursor")})
            hooks[event] = entries
            installed_events.append(event)
    else:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"{settings_path}: 'hooks' must be an object")
        skip_session_start = (
            client == "commandcode" and _has_hook_name(hooks, "SessionStart", LEGACY_CC_SESSION_KEY)
        )
        if not skip_session_start:
            _upsert_named_hook_event(
                hooks,
                "SessionStart",
                BRIEFING_HOOK_NAME,
                _build_hook_command(exe, "briefing", client),
            )
            installed_events.append("SessionStart")
        if include_stop:
            _upsert_named_hook_event(
                hooks,
                "Stop",
                CAPTURE_HOOK_NAME,
                _build_hook_command(exe, "capture", client),
            )
            installed_events.append("Stop")

    backup: Optional[Path] = None
    if not dry_run:
        backup = _backup_first_write(settings_path)
        _atomic_write_json(settings_path, data)

    return {
        "action": "install",
        "client": client,
        "settings_path": str(settings_path),
        "installed_events": installed_events,
        "backup": str(backup) if backup else None,
        "dry_run": dry_run,
    }


def uninstall_client_hooks(
    client: str,
    settings_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove only Engraphis hooks managed by ``engraphis hooks`` without touching others."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"unsupported client: {client!r}; expected one of {SUPPORTED_CLIENTS}")
    if not settings_path.exists():
        return {
            "action": "uninstall",
            "client": client,
            "settings_path": str(settings_path),
            "removed": 0,
            "dry_run": dry_run,
        }

    data = _read_json_object(settings_path)
    hooks = data.get("hooks")
    removed = 0
    if isinstance(hooks, dict):
        if client == "cursor":
            for event in ("sessionStart", "afterAgentResponse", "stop"):
                entries = hooks.get(event)
                if isinstance(entries, list):
                    kept = [e for e in entries if not _is_engraphis_cursor_entry(e)]
                    removed += len(entries) - len(kept)
                    if kept:
                        hooks[event] = kept
                    else:
                        del hooks[event]
        else:
            removed += _remove_named_hook_event(hooks, "SessionStart", (BRIEFING_HOOK_NAME,))
            removed += _remove_named_hook_event(hooks, "Stop", (CAPTURE_HOOK_NAME,))

    backup: Optional[Path] = None
    if removed > 0 and not dry_run:
        backup = _backup_first_write(settings_path)
        _atomic_write_json(settings_path, data)

    return {
        "action": "uninstall",
        "client": client,
        "settings_path": str(settings_path),
        "removed": removed,
        "backup": str(backup) if backup else None,
        "dry_run": dry_run,
    }
