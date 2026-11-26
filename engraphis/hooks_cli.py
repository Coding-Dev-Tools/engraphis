"""CLI entrypoint for ``engraphis hooks`` (install, briefing, capture).

Lives inside the ``engraphis`` package (invoked as ``python -m engraphis.hooks_cli``)
rather than top-level ``scripts/`` so a target repository with its own ``./scripts/``
folder can never shadow or hijack hook execution.

All heavy ``engraphis.config`` / ``engraphis.service`` imports stay lazy inside command
handlers (and inside the ``--hook-mode`` fail-open boundary) so ``hooks install`` never
imports PyTorch/FastAPI and broken local config never wedges a coding-agent session.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    _pkg_dir = str(Path(__file__).resolve().parent)
    _pkg_root = str(Path(__file__).resolve().parent.parent)
    if sys.path and sys.path[0] == _pkg_dir:
        sys.path[0] = _pkg_root
    elif _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)

from engraphis.hooks import (
    SUPPORTED_CLIENTS,
    build_session_briefing,
    default_client_settings_path,
    extract_durable_candidates,
    extract_text_from_hook_payload,
    install_client_hooks,
    stage_passive_capture,
    uninstall_client_hooks,
)

_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HOOK_BUSY_TIMEOUT_MS = 500
_MAX_HOOK_INPUT_BYTES = 1_048_576


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _infer_repo_name(cwd: Optional[str] = None) -> Optional[str]:
    base = Path(cwd).expanduser() if cwd else Path.cwd()
    try:
        cur = base.resolve()
    except OSError:
        return None
    for candidate in (cur,) + tuple(cur.parents):
        if (candidate / ".git").exists():
            name = candidate.name or ""
            if _SAFE_REPO_RE.fullmatch(name):
                return name
            sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-_")[:128]
            return sanitized if sanitized and _SAFE_REPO_RE.fullmatch(sanitized) else None
    return None


def _is_missing_local_db(db_path: str) -> bool:
    if db_path == ":memory:" or db_path.startswith("file:"):
        return False
    return not Path(db_path).expanduser().is_file()


def _resolve_workspace_name(
    service: Any, explicit: Optional[str], *, repo: Optional[str], for_write: bool
) -> str:
    if explicit:
        return explicit
    resolved = service.resolve_workspace(None, repo=repo, for_write=for_write)
    if isinstance(resolved, dict):
        return str(resolved.get("workspace") or "default")
    return str(resolved or "default")


def _read_bounded_stdin() -> str:
    try:
        return sys.stdin.read(_MAX_HOOK_INPUT_BYTES)
    except OSError:
        return ""


def _read_bounded_file(path_str: str) -> str:
    path = Path(path_str).expanduser()
    if path.is_symlink():
        raise ValueError(f"refusing to read symlinked capture input: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"capture input must be a regular file: {path}")
        chunks: list[bytes] = []
        remaining = _MAX_HOOK_INPUT_BYTES
        while remaining > 0:
            block = os.read(fd, min(remaining, 16384))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _open_service(
    db_override: Optional[str],
    *,
    hook_mode: bool = False,
    needs_embeddings: bool = True,
    read_only: bool = False,
) -> tuple[Any, str]:
    from engraphis.config import settings
    from engraphis.core.query_planner import DeterministicQueryPlanner
    from engraphis.service import MemoryService

    db_path = str(db_override) if db_override is not None else str(settings.db_path)
    resolved_db = (
        str(Path(db_path).expanduser())
        if db_path != ":memory:" and not db_path.startswith("file:")
        else db_path
    )
    raw_embed_dim = getattr(settings, "embed_dim", None)
    embed_dim = int(raw_embed_dim) if raw_embed_dim else 384
    embed_model = (settings.embed_model or None) if needs_embeddings else None
    # Pin all passive-hook service opens strictly offline/local so remote LLM/Jev settings
    # never egress unreviewed transcripts or stall synchronous IDE hooks.
    service = MemoryService.create(
        resolved_db,
        embed_model=embed_model,
        embed_dim=embed_dim,
        embed_revision=(getattr(settings, "embed_revision", None) or None),
        require_immutable_models=getattr(settings, "require_immutable_models", False),
        vector_backend=getattr(settings, "vector_backend", "auto"),
        rerank_model=(getattr(settings, "rerank_model", None) or None) if needs_embeddings else None,
        rerank_revision=(getattr(settings, "rerank_revision", None) or None),
        allowed_workspaces=getattr(settings, "allowed_workspaces", None),
        extractor="none",
        graph_extractor="none",
        retention_supervisor="none",
        allow_automatic_critical_retention=False,
        query_planner=DeterministicQueryPlanner(),
        read_only=read_only,
    )
    if hook_mode:
        conn = getattr(getattr(service, "store", None), "conn", None)
        if conn is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout = {_HOOK_BUSY_TIMEOUT_MS}")
            except Exception:
                pass
    return service, resolved_db


def _cmd_install(args: argparse.Namespace) -> int:
    settings_path = (
        Path(args.settings).expanduser()
        if args.settings
        else default_client_settings_path(args.client)
    )
    if args.uninstall:
        result = uninstall_client_hooks(args.client, settings_path, dry_run=args.dry_run)
    else:
        result = install_client_hooks(
            args.client,
            settings_path,
            include_stop=not args.no_stop,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, indent=2))
    return 0


def _cmd_briefing(args: argparse.Namespace) -> int:
    raw_stdin = ""
    if args.hook_mode and not sys.stdin.isatty():
        raw_stdin = _read_bounded_stdin()
    _, payload_meta = extract_text_from_hook_payload(raw_stdin) if raw_stdin else ("", {})
    cwd_hint = str(payload_meta.get("cwd") or "") or None
    repo = args.repo or _infer_repo_name(cwd_hint)

    from engraphis.config import settings

    db_path = str(args.db) if args.db is not None else str(settings.db_path)
    # Never create a brand-new empty DB on disk during a read-only SessionStart briefing.
    if _is_missing_local_db(db_path):
        if args.hook_mode:
            return 0
        print(json.dumps({"workspace": args.workspace or "default", "repo": repo, "count": 0, "context": ""}))
        return 0

    service, _ = _open_service(
        args.db,
        hook_mode=args.hook_mode,
        needs_embeddings=bool((args.query or "").strip()),
        read_only=True,
    )
    try:
        workspace = _resolve_workspace_name(service, args.workspace, repo=repo, for_write=False)
        result = build_session_briefing(
            service,
            workspace=workspace,
            repo=repo,
            query=args.query or "",
            max_chars=args.max_chars,
            client=args.client,
        )
    finally:
        service.close()

    if args.hook_mode:
        if result["hook_output"]:
            print(json.dumps(result["hook_output"]))
        return 0
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["context"]:
        print(result["context"])
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    raw_input = ""
    if args.file:
        raw_input = _read_bounded_file(args.file)
    elif args.stdin or not sys.stdin.isatty():
        raw_input = _read_bounded_stdin()

    extracted_text, payload_meta = extract_text_from_hook_payload(raw_input)
    if payload_meta.get("skipped_stop_loop") or not extracted_text.strip():
        if not args.hook_mode:
            print(json.dumps({"extracted": 0, "stored_count": 0, "skipped_stop_loop": bool(payload_meta.get("skipped_stop_loop"))}))
        return 0

    # Short-circuit BEFORE loading MemoryService or neural embeddings if no durable candidates exist.
    if not extract_durable_candidates(extracted_text, max_candidates=args.max_candidates):
        if not args.hook_mode:
            print(json.dumps({"extracted": 0, "stored_count": 0, "skipped_stop_loop": False}))
        return 0

    from engraphis.config import settings

    cwd_hint = str(payload_meta.get("cwd") or "") or None
    repo = args.repo or _infer_repo_name(cwd_hint)
    db_path = str(args.db) if args.db is not None else str(settings.db_path)
    if args.hook_mode and _is_missing_local_db(db_path):
        return 0

    service, _ = _open_service(args.db, hook_mode=args.hook_mode, needs_embeddings=True)
    try:
        workspace = _resolve_workspace_name(service, args.workspace, repo=repo, for_write=True)
        result = stage_passive_capture(
            service,
            extracted_text,
            workspace=workspace,
            repo=repo,
            secret_mode=args.secret_mode,
            max_candidates=args.max_candidates,
            dry_run=args.dry_run,
        )
    finally:
        service.close()

    if args.hook_mode:
        return 0
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engraphis hooks",
        description="Install and run Engraphis passive coding-agent memory hooks.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_inst = sub.add_parser("install", help="Install or uninstall client hooks idempotently.")
    p_inst.add_argument("--client", choices=SUPPORTED_CLIENTS, required=True)
    p_inst.add_argument("--settings", default=None, help="Override client settings/hooks JSON path.")
    p_inst.add_argument("--no-stop", action="store_true", help="Install SessionStart briefing only.")
    p_inst.add_argument("--uninstall", action="store_true", help="Remove Engraphis hooks.")
    p_inst.add_argument("--dry-run", action="store_true", help="Preview changes without writing.")

    p_brief = sub.add_parser("briefing", help="Emit a bounded turn-0 durable memory briefing.")
    p_brief.add_argument("--db", default=None, help="Override SQLite database path.")
    p_brief.add_argument("--workspace", default=None, help="Target workspace name.")
    p_brief.add_argument("--repo", default=None, help="Target repository name.")
    p_brief.add_argument("--query", default="", help="Optional scoping query.")
    p_brief.add_argument("--max-chars", type=int, default=1200)
    p_brief.add_argument("--client", choices=SUPPORTED_CLIENTS, default="claude")
    p_brief.add_argument("--hook-mode", action="store_true", help="Fail-open JSON hook stdout mode.")
    p_brief.add_argument("--json", action="store_true")

    p_cap = sub.add_parser("capture", help="Stage durable candidates from Stop hook or text.")
    p_cap.add_argument("--db", default=None, help="Override SQLite database path.")
    p_cap.add_argument("--workspace", default=None, help="Target workspace name.")
    p_cap.add_argument("--repo", default=None, help="Target repository name.")
    p_cap.add_argument("--file", default=None, help="Read candidate text from a file.")
    p_cap.add_argument("--stdin", action="store_true", help="Read candidate text from stdin.")
    p_cap.add_argument("--secret-mode", choices=("reject", "redact"), default="reject")
    p_cap.add_argument("--max-candidates", type=int, default=5)
    p_cap.add_argument("--client", choices=SUPPORTED_CLIENTS, default="claude")
    p_cap.add_argument("--hook-mode", action="store_true", help="Fail-open mode for IDE hooks.")
    p_cap.add_argument("--dry-run", action="store_true")
    p_cap.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    hook_mode = bool(getattr(args, "hook_mode", False))
    try:
        if args.subcommand == "install":
            return _cmd_install(args)
        if args.subcommand == "briefing":
            return _cmd_briefing(args)
        if args.subcommand == "capture":
            return _cmd_capture(args)
        return 2
    except Exception as exc:
        if hook_mode:
            # Fail-open inside IDE hooks so local locks or bad config never wedge a turn.
            return 0
        print(f"engraphis hooks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
