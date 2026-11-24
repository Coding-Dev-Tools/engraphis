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
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from engraphis.hooks import (
    SUPPORTED_CLIENTS,
    build_session_briefing,
    default_client_settings_path,
    extract_text_from_hook_payload,
    install_client_hooks,
    stage_passive_capture,
    uninstall_client_hooks,
)


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
            return candidate.name or None
    return None


def _resolve_db_and_embed_model(db_override: Optional[str]) -> tuple[str, Optional[str]]:
    from engraphis.config import settings

    db_path = str(db_override) if db_override is not None else str(settings.db_path)
    embed_model = settings.embed_model or None
    return db_path, embed_model


def _open_service(db_path: str, embed_model: Optional[str]) -> Any:
    from engraphis.service import MemoryService

    return MemoryService.create(db_path, embed_model=embed_model)


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
        try:
            raw_stdin = sys.stdin.read()
        except OSError:
            raw_stdin = ""
    _, payload_meta = extract_text_from_hook_payload(raw_stdin) if raw_stdin else ("", {})
    cwd_hint = str(payload_meta.get("cwd") or "") or None
    repo = args.repo or _infer_repo_name(cwd_hint)

    db_path, embed_model = _resolve_db_and_embed_model(args.db)
    # Never create a brand-new empty DB on disk during a read-only SessionStart briefing.
    if db_path != ":memory:" and not db_path.startswith("file:") and not Path(db_path).is_file():
        if args.hook_mode:
            return 0
        print(json.dumps({"workspace": args.workspace or "default", "repo": repo, "count": 0, "context": ""}))
        return 0

    service = _open_service(db_path, embed_model)
    try:
        workspace = args.workspace or service.resolve_workspace(None, repo=repo, for_write=False)
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
        raw_input = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif args.stdin or args.hook_mode or not sys.stdin.isatty():
        raw_input = sys.stdin.read()

    extracted_text, payload_meta = extract_text_from_hook_payload(raw_input)
    if payload_meta.get("skipped_stop_loop") or not extracted_text.strip():
        if not args.hook_mode:
            print(json.dumps({"extracted": 0, "stored_count": 0, "skipped_stop_loop": bool(payload_meta.get("skipped_stop_loop"))}))
        return 0

    cwd_hint = str(payload_meta.get("cwd") or "") or None
    repo = args.repo or _infer_repo_name(cwd_hint)
    db_path, embed_model = _resolve_db_and_embed_model(args.db)
    if args.hook_mode and db_path != ":memory:" and not db_path.startswith("file:") and not Path(db_path).is_file():
        return 0

    service = _open_service(db_path, embed_model)
    try:
        workspace = args.workspace or service.resolve_workspace(None, repo=repo, for_write=True)
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
