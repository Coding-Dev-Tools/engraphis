#!/usr/bin/env python3
"""Offline-first v2 source importer command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Optional

from engraphis.config import settings
from engraphis.core.documents import scan_document_tree
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.obsidian import scan_obsidian_vault
from engraphis.core.store import Store
from engraphis.document_import import DocumentImporter, local_document_adapter
from engraphis.obsidian_import import ObsidianImporter
from engraphis.service import MemoryService


_CONSOLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _console(value: object, *, file=None) -> None:
    """Print untrusted source labels without control injection or codec failures."""
    stream = file or sys.stdout
    rendered = _CONSOLE_CONTROL_RE.sub(
        lambda match: "\\x%02x" % ord(match.group(0)), str(value),
    )
    encoding = getattr(stream, "encoding", None) or "utf-8"
    rendered = rendered.encode(encoding, errors="backslashreplace").decode(encoding)
    print(rendered, file=stream)


def _json(value: object) -> None:
    # JSON escapes stay lossless on Windows consoles using a legacy charmap.
    print(json.dumps(
        value, ensure_ascii=True, sort_keys=True, default=str, allow_nan=False,
    ))


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engraphis import", description="Import a local source into Engraphis v2.",
    )
    sub = parser.add_subparsers(dest="source", required=True)
    documents = sub.add_parser(
        "documents",
        help="import a mixed local folder of Markdown, text, and documents",
    )
    _source_arguments(documents, path_help="path to the local document collection")
    documents.add_argument(
        "--source-id", help="reuse a registered vlt_ source collection identity",
    )
    documents.add_argument(
        "--source-label", default="", help="display label for a new source collection",
    )

    obsidian = sub.add_parser(
        "obsidian", help="import an Obsidian Markdown vault (compatibility command)",
    )
    _source_arguments(obsidian, path_help="path to the Obsidian vault")
    obsidian.add_argument("--vault-id", help="reuse a registered vlt_ identity")
    obsidian.add_argument("--vault-label", default="", help="display label for a new vault")
    return parser


def _source_arguments(parser: argparse.ArgumentParser, *, path_help: str) -> None:
    """Add the transport-neutral v2 import options shared by all adapters."""
    parser.add_argument("path", help=path_help)
    parser.add_argument("--db", default=settings.db_path, help="v2 database path")
    parser.add_argument("--workspace", help="target workspace name")
    parser.add_argument(
        "--repo",
        help="target repository name (must match --session when both are supplied)",
    )
    parser.add_argument(
        "--session", dest="session_id",
        help="active target session ID (defaults the import scope to session)",
    )
    parser.add_argument("--scope", choices=("workspace", "repo", "session"))
    parser.add_argument(
        "--memory-type", default="semantic",
        choices=("working", "episodic", "semantic", "procedural"),
    )
    parser.add_argument("--dry-run", action="store_true", help="preview with zero database writes")
    parser.add_argument(
        "--on-conflict", default="error", choices=("error", "replace", "new"),
        help="divergent lineage policy (default: error)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm this trusted-local import")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--limit", type=_nonnegative_int, default=0, metavar="N",
        help=(
            "process at most N documents in this run, then "
            "leave it resumable (0 = all)"
        ),
    )


def _scope(args: argparse.Namespace) -> Scope:
    scope = Scope(args.scope) if args.scope else (
        Scope.SESSION if args.session_id
        else Scope.REPO if args.repo
        else Scope.WORKSPACE
    )
    if scope == Scope.SESSION and not args.session_id:
        raise ValueError("session scope requires --session")
    if scope == Scope.REPO and not (args.repo or args.session_id):
        raise ValueError("repo scope requires --repo (or a repo-backed --session)")
    if scope == Scope.WORKSPACE and (args.repo or args.session_id):
        raise ValueError("workspace scope requires --repo and --session to be omitted")
    return scope


def _workspace(args: argparse.Namespace, source: Path) -> str:
    if args.workspace:
        workspace = str(args.workspace).strip()
        if not workspace:
            raise ValueError("--workspace must not be blank")
        return workspace
    if sys.stdin.isatty():
        default = source.name or "documents"
        entered = input(f"Target workspace [{default}]: ").strip()
        return entered or default
    raise ValueError("--workspace is required in non-interactive mode")


def _snapshot_target(
    snapshot: dict, *, root_digest: str, workspace: str,
    repo: Optional[str], session_id: Optional[str], vault_id: Optional[str],
    source_kind: str,
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    vaults = list(snapshot.get("vaults") or [])
    if vault_id:
        selected = next((row for row in vaults if row.get("id") == vault_id), None)
        if selected is None:
            raise ValueError("registered source was not found in the read-only manifest")
        if selected.get("kind") != source_kind:
            raise ValueError("registered source uses a different import adapter")
        if selected.get("workspace_name") != workspace:
            raise ValueError("registered vault belongs to another workspace")
        if (selected.get("repo_name") or None) != repo or selected.get("session_id") != session_id:
            raise ValueError("registered vault has a different target scope")
        if selected.get("root_digest") != root_digest:
            raise ValueError("selected path does not match the registered vault")
        return selected, selected.get("workspace_id"), selected.get("repo_id")
    matches = [
        row for row in vaults
        if row.get("kind") == source_kind
        and row.get("root_digest") == root_digest
        and row.get("workspace_name") == workspace
        and (row.get("repo_name") or None) == repo
        and row.get("session_id") == session_id
    ]
    selected = matches[0] if len(matches) == 1 else None
    return (
        selected,
        selected.get("workspace_id") if selected else None,
        selected.get("repo_id") if selected else None,
    )


def _effective_manifest_repo(
    snapshot: dict, *, repo: Optional[str], session_id: Optional[str],
) -> Optional[str]:
    """Use a session-backed manifest repository when the CLI omitted ``--repo``."""
    if repo is not None or session_id is None:
        return repo
    session_repos = {
        str(row["repo_name"])
        for row in snapshot.get("vaults") or []
        if row.get("session_id") == session_id and row.get("repo_name")
    }
    session_repos.update(
        str(row["repo_name"])
        for row in snapshot.get("sessions") or []
        if row.get("id") == session_id and row.get("repo_name")
    )
    if len(session_repos) > 1:
        raise ValueError("session maps to multiple repositories in the import manifest")
    return next(iter(session_repos), None)


def _effective_manifest_target(
    snapshot: dict, *, repo: Optional[str], session_id: Optional[str], scope: Scope,
) -> tuple[Optional[str], Optional[str]]:
    """Normalize the manifest target the same way the service normalizes imports."""
    effective_repo = _effective_manifest_repo(
        snapshot, repo=repo, session_id=session_id,
    )
    effective_session = None if scope == Scope.REPO else session_id
    return effective_repo, effective_session


def _preview(args: argparse.Namespace, scan, workspace: str) -> dict:
    snapshot = _manifest_snapshot(args.db)
    scope = _scope(args)
    effective_repo, effective_session = _effective_manifest_target(
        snapshot, repo=args.repo, session_id=args.session_id, scope=scope,
    )
    selected, workspace_id, repo_id = _snapshot_target(
        snapshot, root_digest=scan.vault_id, workspace=workspace,
        repo=effective_repo, session_id=effective_session, vault_id=args.vault_id,
        source_kind="obsidian",
    )
    report = ObsidianImporter().preview(
        scan, workspace_id=workspace_id, repo_id=repo_id,
        session_id=effective_session, scope=scope,
        memory_type=MemoryType(args.memory_type),
        vault_id=selected.get("id") if selected else None,
        vault_label=args.vault_label or Path(args.path).name,
        on_conflict=args.on_conflict,
        manifest=(
            snapshot if selected is not None
            else {"vaults": [], "items": []}
        ),
    )
    report["target"].update({"workspace": workspace, "repo": effective_repo})
    return report


def _preview_documents(args: argparse.Namespace, scan, workspace: str) -> dict:
    snapshot = _manifest_snapshot(args.db)
    scope = _scope(args)
    effective_repo, effective_session = _effective_manifest_target(
        snapshot, repo=args.repo, session_id=args.session_id, scope=scope,
    )
    selected, workspace_id, repo_id = _snapshot_target(
        snapshot, root_digest=scan.source_id, workspace=workspace,
        repo=effective_repo, session_id=effective_session, vault_id=args.source_id,
        source_kind="documents",
    )
    report = DocumentImporter().preview(
        scan, workspace_id=workspace_id, repo_id=repo_id,
        session_id=effective_session, scope=scope,
        memory_type=MemoryType(args.memory_type),
        source_id=selected.get("id") if selected else None,
        source_label=args.source_label or Path(args.path).name,
        on_conflict=args.on_conflict,
        manifest=(
            snapshot if selected is not None
            else {"vaults": [], "items": []}
        ),
    )
    report.setdefault("adapter", "documents")
    report.setdefault("source_adapter", "documents")
    report["target"].update({"workspace": workspace, "repo": effective_repo})
    return report


def _manifest_snapshot(db_path: str) -> dict:
    """Read plaintext or SQLCipher manifests without migrations or sidecar writes."""
    from engraphis.backends.encrypted_db import connector_from_env

    return Store.snapshot_source_import_manifest(
        db_path, connect=connector_from_env(),
    )


def _print_human(report: dict, *, heading: str) -> None:
    counts = report.get("counts", {})
    summary = report.get("summary", {})
    _console("")
    _console(heading)
    _console(
        "Documents: {documents} | import: {imported} | update: {updated} | "
        "rename: {renamed} | skip: {skipped} | reject: {rejected} | "
        "conflict: {conflict} | missing: {missing}".format(
            **{
                "documents": int(counts.get("documents", counts.get("markdown", 0))),
                **{key: int(counts.get(key, 0)) for key in (
                "imported", "updated", "renamed", "skipped",
                "rejected", "conflict", "missing",
            )}}
        )
    )
    formats = summary.get("formats") or {}
    format_text = ", ".join(
        f"{name}: {count}" for name, count in sorted(formats.items())
    ) if isinstance(formats, dict) else ""
    _console(
        f"Detected: {len(summary.get('folders', []))} folders"
        + (f", formats [{format_text}]" if format_text else "") + ", "
        f"{len(summary.get('tags', []))} tags, {summary.get('aliases', 0)} aliases, "
        f"{summary.get('wikilinks', 0)} wikilinks, "
        f"{summary.get('attachments', 0)} attachment references, "
        f"{summary.get('warnings', 0)} warnings"
    )
    for row in report.get("files", []):
        status = str(row.get("status") or "reported").upper()
        path = row.get("relative_path") or "(vault)"
        reason = row.get("reason") or ""
        detected_format = row.get("format") or row.get("source_format") or ""
        format_suffix = f" [{detected_format}]" if detected_format else ""
        _console(
            f"  {status:9} {path}{format_suffix}"
            + (f" — {reason}" if reason else "")
        )
        for warning in row.get("warnings", [])[:5]:
            _console(f"             warning: {warning}")


def _local_service(db_path: str) -> MemoryService:
    embed_model = str(settings.embed_model or "").strip()
    if embed_model and not embed_model.startswith("local:"):
        embed_model = "local:" + embed_model
    service = MemoryService.create(
        db_path, embed_model=embed_model or None,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim or 384, vector_backend=settings.vector_backend,
        rerank_model=None,
        extractor="none", graph_extractor="none", retention_supervisor="none",
    )
    if embed_model:
        from engraphis.backends.embedder_st import LAST_EMBEDDER_ERROR

        if LAST_EMBEDDER_ERROR:
            service.close()
            raise RuntimeError(
                "the configured embedding model is not available in the local cache; "
                "the importer will not download it"
            )
    return service


def _confirm(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if args.json:
        raise ValueError("JSON-mode imports require --yes")
    if not sys.stdin.isatty():
        raise ValueError("non-interactive imports require --yes")
    answer = input(
        "Import these local documents as trusted canonical memories? [y/N]: "
    ).strip().casefold()
    return answer in {"y", "yes"}


def _run_obsidian(args: argparse.Namespace) -> int:
    vault = Path(args.path).expanduser()
    workspace = _workspace(args, vault)
    scan = scan_obsidian_vault(vault)
    preview = _preview(args, scan, workspace)
    if args.dry_run:
        if args.json:
            _json(preview)
        else:
            _print_human(preview, heading="Obsidian import preview (no writes)")
        counts = preview.get("counts", {})
        return 3 if counts.get("conflict") or counts.get("rejected") else 0
    if not args.json:
        _print_human(preview, heading="Obsidian import preview")
    effective_limit = args.limit if 0 < args.limit < len(scan.notes) else 0
    if effective_limit and not args.json:
        _console(
            f"Legacy limit: this run will pause after {effective_limit} notes. "
            "Rerun without --limit to finish reconciliation."
        )
    if not _confirm(args):
        if not args.json:
            _console("Import cancelled; no memories were written.")
        return 130

    service = _local_service(args.db)
    progress_count = 0

    def progress(row: dict) -> None:
        nonlocal progress_count
        progress_count += 1
        if not args.json:
            _console(
                f"[{progress_count}/{len(scan.notes)}] "
                f"{str(row.get('status', 'reported')).upper():9} "
                f"{row.get('relative_path', '')}"
            )

    try:
        report = service.import_obsidian_vault(
            str(vault), workspace=workspace, repo=args.repo,
            session_id=args.session_id, scope=_scope(args).value,
            memory_type=args.memory_type, vault_id=args.vault_id,
            vault_label=args.vault_label or vault.name,
            on_conflict=args.on_conflict, confirmed=True,
            actor="local_cli_operator", progress=progress,
            _scan=scan,
            cancel_check=(
                (lambda: progress_count >= effective_limit)
                if effective_limit else None
            ),
        )
    finally:
        service.close()
    if args.json:
        payload = {"preview": preview, "report": report}
        if args.limit:
            payload["limit"] = {
                "requested": args.limit,
                "processed": progress_count,
                "reached": bool(effective_limit and progress_count >= effective_limit),
            }
        _json(payload)
    else:
        _print_human(report, heading=f"Obsidian import {report.get('state', 'complete')}")
        if effective_limit and progress_count >= effective_limit:
            _console("Paused at --limit; the import remains resumable.")
    if (
        effective_limit
        and progress_count >= effective_limit
        and str(report.get("state")) == "cancelled"
    ):
        return 3
    return {
        "completed": 0, "partial": 3, "failed": 3, "cancelled": 130,
    }.get(str(report.get("state")), 3)


def _run_documents(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser()
    workspace = _workspace(args, root)
    scan = scan_document_tree(root, adapter=local_document_adapter)
    preview = _preview_documents(args, scan, workspace)
    if args.dry_run:
        if args.json:
            _json(preview)
        else:
            _print_human(preview, heading="Document import preview (no writes)")
        counts = preview.get("counts", {})
        return 3 if counts.get("conflict") or counts.get("rejected") else 0
    if not args.json:
        _print_human(preview, heading="Document import preview")
    documents = list(getattr(scan, "documents", ()))
    effective_limit = args.limit if 0 < args.limit < len(documents) else 0
    if effective_limit and not args.json:
        _console(
            f"This run will pause after {effective_limit} documents. "
            "Rerun without --limit to finish reconciliation."
        )
    if not _confirm(args):
        if not args.json:
            _console("Import cancelled; no memories were written.")
        return 130

    service = _local_service(args.db)
    progress_count = 0

    def progress(row: dict) -> None:
        nonlocal progress_count
        progress_count += 1
        if not args.json:
            _console(
                f"[{progress_count}/{len(documents)}] "
                f"{str(row.get('status', 'reported')).upper():9} "
                f"{row.get('relative_path', '')}"
            )

    try:
        report = service.import_document_tree(
            str(root), workspace=workspace, repo=args.repo,
            session_id=args.session_id, scope=_scope(args).value,
            memory_type=args.memory_type, source_id=args.source_id,
            source_label=args.source_label or root.name,
            on_conflict=args.on_conflict, confirmed=True,
            actor="local_cli_operator", progress=progress,
            _scan=scan,
            cancel_check=(
                (lambda: progress_count >= effective_limit)
                if effective_limit else None
            ),
        )
    finally:
        service.close()
    if args.json:
        payload = {"preview": preview, "report": report}
        if args.limit:
            payload["limit"] = {
                "requested": args.limit,
                "processed": progress_count,
                "reached": bool(effective_limit and progress_count >= effective_limit),
            }
        _json(payload)
    else:
        _print_human(report, heading=f"Document import {report.get('state', 'complete')}")
        if effective_limit and progress_count >= effective_limit:
            _console("Paused at --limit; the import remains resumable.")
    if (
        effective_limit
        and progress_count >= effective_limit
        and str(report.get("state")) == "cancelled"
    ):
        return 3
    return {
        "completed": 0, "partial": 3, "failed": 3, "cancelled": 130,
    }.get(str(report.get("state")), 3)


def main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.source == "documents":
            return _run_documents(args)
        if args.source == "obsidian":
            return _run_obsidian(args)
        parser.error("unsupported import source")
    except KeyboardInterrupt:
        _console("Import cancelled.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        _console(f"engraphis import: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
