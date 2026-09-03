"""CLI — quick interactive access to your memory system, no server required.

Talks directly to the v2 :class:`~engraphis.service.MemoryService` (the same
validated facade the MCP server and Inspector use) against ``ENGRAPHIS_DB_PATH``,
so every command works offline with nothing else running. The ``--namespace``
flag maps onto a v2 *workspace*.

Usage:
    engraphis-cli ingest "User prefers dark mode" --namespace preferences --key theme
    engraphis-cli ingest-file notes.md --namespace vault
    engraphis-cli recall "What does the user prefer?" --namespace preferences
    engraphis-cli chat "What do you know about Alice?"
    engraphis-cli list --namespace vault
    engraphis-cli delete-namespace vault
"""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
from pathlib import Path

from engraphis.config import settings
from engraphis.core.interfaces import SearchFilter
from engraphis.core.poisoning import REVIEW_APPROVED, REVIEW_PENDING, inspection_eligible
from engraphis.core.store import now_ts
from engraphis.service import MemoryService, ValidationError


def _emit_update_notice() -> None:
    """Remind terminal users about a newer release without affecting command output."""
    try:
        from engraphis import update_check

        update_check.emit_cli_notice()
    except Exception:  # noqa: BLE001 - updates are optional and fail-silent
        pass


class _ServiceStartupError(Exception):
    """Marks a failure raised while constructing the MemoryService itself.

    Command-phase failures of the same builtin types (a bad input path, a
    locked database mid-operation) must not be mislabeled as startup problems,
    so construction is wrapped in this dedicated marker and main() maps only
    this type through _startup_error.
    """

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(str(original))


def _service() -> MemoryService:
    try:
        return MemoryService.create(
            settings.db_path,
            embed_model=settings.embed_model or None,
            embed_revision=getattr(settings, "embed_revision", "") or None,
            require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
            embed_dim=settings.embed_dim or 384,
            vector_backend=settings.vector_backend,
            rerank_model=getattr(settings, "rerank_model", "") or None,
            rerank_revision=getattr(settings, "rerank_revision", "") or None,
            extractor=settings.extractor,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as the startup marker
        raise _ServiceStartupError(exc) from exc


def _metadata_object(value: str) -> dict:
    """Parse CLI metadata as a JSON object, never as a scalar or sequence."""
    try:
        metadata = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise argparse.ArgumentTypeError("metadata must be a valid JSON object") from exc
    if not isinstance(metadata, dict):
        raise argparse.ArgumentTypeError("metadata must be a JSON object")
    return metadata


def cmd_ingest(args: argparse.Namespace) -> None:
    # A terminal command is the local database owner's explicit memory action. It is
    # not a public transport assertion, so use the service's narrow local-owner path;
    # normal HTTP/MCP/import writes remain pending review by design.
    svc = _service()
    try:
        out = svc.remember_local_cli(
            args.content,
            workspace=args.namespace,
            title=args.key or "",
            metadata=(args.metadata or {}) | {"source": "cli"},
        )
    finally:
        svc.close()
    print(f"Stored: {out['id']} (workspace={out['workspace']}, op={out['op']})")
    if out.get("resolution"):
        print(f"  resolution: {out['resolution']}")


def cmd_ingest_file(args: argparse.Namespace) -> None:
    p = Path(args.file)
    if not p.exists():
        print(f"File not found: {p}")
        sys.exit(1)
    content = p.read_text(encoding="utf-8", errors="replace")
    doc_id = args.key or p.stem
    svc = _service()
    try:
        out = svc.ingest(
            content,
            workspace=args.namespace,
            metadata={"source": "cli", "document_id": doc_id, "file": p.name},
            source="cli",
        )
    finally:
        svc.close()
    print(f"Stored '{p.name}' as {doc_id} ({len(content)} chars, "
          f"{out['count']} memories, extracted={out['extracted']})")


def cmd_recall(args: argparse.Namespace) -> None:
    svc = _service()
    try:
        out = svc.recall(args.prompt, workspace=args.namespace, k=args.num_chunks)
    finally:
        svc.close()
    if not out["count"]:
        print(f"(no memories found{': ' + out['note'] if out.get('note') else ''})")
        return
    print(f"Found {out['count']} memories:\n")
    print(out["context"])


def cmd_chat(args: argparse.Namespace) -> None:
    # Grounded, citation-backed answer built strictly from stored memories —
    # offline and deterministic (no LLM/API key needed, unlike the old REST chat).
    svc = _service()
    try:
        out = svc.grounded_recall(args.prompt, workspace=args.namespace)
    finally:
        svc.close()
    if not out.get("grounded"):
        print(f"(no grounded answer: {out.get('reason') or 'insufficient supporting memories'})")
        return
    print(out["answer"])
    for i, c in enumerate(out.get("citations", []), start=1):
        print(f"  [{i}] {c.get('title') or c.get('content', '')[:80]}")



def cmd_list(args: argparse.Namespace) -> None:
    svc = _service()
    try:
        out = svc.recall_proactive(workspace=args.namespace, k=args.limit)
    finally:
        svc.close()
    if not out["memories"]:
        print("(no memories)")
        return
    for m in out["memories"]:
        title = (m["title"] or m["content"])[:60]
        print(f"  [{m['id']}] {title}  "
              f"({m['mtype']}, importance={m['importance']:.2f}"
              f"{', pinned' if m['pinned'] else ''})")


def cmd_delete_ns(args: argparse.Namespace) -> None:
    ns = args.namespace or args.namespace_flag
    if not ns:
        print("Error: delete-namespace requires a namespace (positional or --namespace/--workspace).")
        sys.exit(2)
    if not args.force:
        print(
            f"This will retire ALL memories in namespace '{ns}'. "
            "Use --force to confirm."
        )
        sys.exit(1)
    svc = _service()
    connection = svc.store.conn
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            wid, _ = svc._require_scope(ns, None)
            retired_at = now_ts()
            rows = connection.execute(
                "SELECT id FROM memories "
                "WHERE workspace_id=? AND expired_at IS NULL "
                "AND (valid_to IS NULL OR valid_to>?) ORDER BY id",
                (wid, retired_at),
            ).fetchall()
            # Authorize the complete snapshot before the first mutation. Session-private
            # rows still require their owner, and any failure must leave the batch intact.
            for row in rows:
                svc._check_owns(row["id"], wid, None)
            for row in rows:
                svc.store.close_validity(
                    row["id"], at=retired_at, actor="cli",
                    reason="cli delete-namespace", commit=False,
                )
            svc.store.record_receipt(
                "retire", workspace_id=wid, actor="cli",
                target_count=len(rows), status="ok",
                metadata={"mode": "namespace_batch", "result_count": len(rows)},
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        print(
            f"Retired {len(rows)} memories from '{ns}' "
            "(audited soft-retirement)"
        )
    finally:
        svc.store.close()


def _pending_review_candidates(args: argparse.Namespace, service: MemoryService) -> list:
    """Return live, non-quarantined pending rows without exposing their content."""
    workspace_id = service._lookup_workspace(args.namespace)
    if workspace_id is None:
        return []
    repo_id = None
    if getattr(args, "repo", None):
        repo_id = service._lookup_repo(workspace_id, args.repo)
        if repo_id is None:
            return []
    scope_filter = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
    records = service.store.list_memories(
        scope_filter,
        include_invalid=False,
    )
    history = service.store.list_memories(
        scope_filter,
        include_invalid=True,
    )
    approved_sources = {
        str(record.provenance.get("approved_from"))
        for record in history
        if record.provenance.get("review_state") == REVIEW_APPROVED
        and record.provenance.get("approved_from")
    }
    sources = set(getattr(args, "source", None) or [])
    legacy_only = bool(getattr(args, "legacy_agent_only", False))
    candidates = []
    for record in records:
        provenance = record.provenance or {}
        if record.id in approved_sources:
            continue
        if provenance.get("review_state") != REVIEW_PENDING:
            continue
        if not inspection_eligible(provenance, record.metadata):
            continue
        if sources and str(provenance.get("source") or "") not in sources:
            continue
        if legacy_only and not (
            provenance.get("source") in {"agent", "intent_api"}
            and provenance.get("trusted") is False
            and provenance.get("trust_origin") == "service_review_gate"
            and provenance.get("trust_downgraded") is True
        ):
            continue
        candidates.append(record)
    return sorted(candidates, key=lambda record: (record.ingested_at or 0.0, record.id))


def cmd_review_list(args: argparse.Namespace) -> None:
    service = _service()
    try:
        limit = max(1, min(10_000, int(args.limit)))
        candidates = _pending_review_candidates(args, service)[:limit]
        if not candidates:
            print("(no pending, non-quarantined memories)")
            return
        for record in candidates:
            provenance = record.provenance or {}
            print(json.dumps({
                "id": record.id,
                "source": str(provenance.get("source") or ""),
                "trust_origin": str(provenance.get("trust_origin") or ""),
                "scope": record.scope.value,
                "mtype": record.mtype.value,
                "ingested_at": record.ingested_at,
            }, sort_keys=True))
        print(f"Pending candidates: {len(candidates)}")
    finally:
        service.store.close()


def cmd_review_approve(args: argparse.Namespace) -> None:
    service = _service()
    try:
        candidates = _pending_review_candidates(args, service)
        by_id = {record.id: record for record in candidates}
        requested = list(dict.fromkeys(args.memory_ids))
        if args.all and requested:
            raise ValidationError("use either memory ids or --all, not both")
        if not args.all and not requested:
            raise ValidationError("provide memory ids or --all")
        if requested:
            missing = [memory_id for memory_id in requested if memory_id not in by_id]
            if missing:
                raise ValidationError(
                    "not a live, pending, non-quarantined candidate in this scope: "
                    + ", ".join(missing)
                )
            selected = [by_id[memory_id] for memory_id in requested]
        else:
            selected = candidates
        if not selected:
            print("(no pending, non-quarantined memories)")
            return
        print(
            f"{'Would approve' if not args.apply else 'Selected'} "
            f"{len(selected)} memories in '{args.namespace}'."
        )
        if not args.apply:
            print("Dry run only; add --apply to create approved successors.")
            return
        if not args.yes:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValidationError("bulk approval requires an interactive TTY or --yes")
            phrase = f"APPROVE {len(selected)}"
            entered = input(f"Type '{phrase}' to approve this batch: ").strip()
            if entered != phrase:
                raise ValidationError("approval confirmation did not match")
        approved = []
        failures = []
        for record in selected:
            try:
                result = service.engine.approve_for_prompt(
                    record.id, reviewer=args.reviewer, reason=args.reason,
                )
                approved.append(result["id"])
            except (KeyError, ValueError):
                failures.append(record.id)
        print(f"Approved {len(approved)} memories.")
        for memory_id in approved:
            print(memory_id)
        if failures:
            raise ValidationError(
                f"{len(failures)} approvals failed after selection: "
                + ", ".join(failures)
            )
    finally:
        service.store.close()

def _startup_error(exc: BaseException) -> str:
    """Map a service-startup failure to one redacted, actionable CLI line.

    Mirrors scripts/start_dashboard.py:_startup_error. Messages stay value-free
    where third-party text could embed credentials or private paths; the
    configured database path itself is owner-visible and safe to name.
    """
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return ("A required dependency is missing. Run engraphis-init --check to see "
                "which optional extra provides it.")
    if isinstance(exc, sqlite3.Error):
        return ("Could not open the Engraphis database — run engraphis-init --check "
                "to verify the configured database path.")
    if isinstance(exc, OSError):
        return (f"File or permission error while starting the service "
                f"({type(exc).__name__}). Run engraphis-init --check for diagnostics.")
    if isinstance(exc, RuntimeError):
        # Backend/provider RuntimeErrors can embed proxy credentials, certificate
        # paths, or endpoint URLs. Redact to the exception type so operator output
        # stays value-free.
        return ("Service initialization failed during backend/model setup. "
                "Run engraphis-init --check for diagnostics.")
    return f"Command failed ({type(exc).__name__})."


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engraphis-cli", description="Engraphis CLI",
        epilog="Works offline against ENGRAPHIS_DB_PATH via the v2 MemoryService — no server "
               "needed. The old --server URL mode (v1 REST /memory/insert|/memory/query) was "
               "removed; point ENGRAPHIS_DB_PATH at the server's database to share its memory. "
               "Defaults matrix: ingest defaults to workspace 'default'; ingest-file defaults "
               "to workspace 'vault'; recall and chat default to all workspaces when no "
               "--namespace/--workspace is given; list defaults to workspace 'default'; "
               "delete-namespace takes the namespace positionally or via --namespace/--workspace.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="Store a text memory")
    p.add_argument("content", help="Memory content text")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default="default",
                   help="Workspace/namespace (default: default)")
    p.add_argument("--key", "-k", help="Document key/ID")
    p.add_argument("--metadata", type=_metadata_object,
                   help="JSON metadata object", default=None)
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("ingest-file", help="Store a file as a memory")
    p.add_argument("file", help="Path to file")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default="vault",
                   help="Workspace/namespace (default: vault)")
    p.add_argument("--key", "-k", help="Document key/ID")
    p.set_defaults(func=cmd_ingest_file)
    p = sub.add_parser("recall", help="Recall memories for a prompt")
    p.add_argument("prompt", help="Query prompt")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default=None,
                   help="Workspace/namespace (default: all workspaces)")
    p.add_argument("--num-chunks", "-c", type=int, default=5)
    p.set_defaults(func=cmd_recall)
    p = sub.add_parser("chat", help="Grounded answer from memory (offline, cited)")
    p.add_argument("prompt", help="Your question")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default=None,
                   help="Workspace/namespace (default: all workspaces)")
    p.set_defaults(func=cmd_chat)
    p = sub.add_parser("list", help="List documents in a namespace")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default="default",
                   help="Workspace/namespace (default: default)")
    p.add_argument("--limit", "-l", type=int, default=20)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("delete-namespace", help="Retire every memory in a namespace")
    p.add_argument("namespace", nargs="?", default=None,
                   help="Namespace whose memories will be retired (or pass --namespace/--workspace)")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace_flag", default=None,
                   help="Workspace/namespace flag alternative to the positional")
    p.add_argument("--force", action="store_true", help="Confirm retirement")
    p.set_defaults(func=cmd_delete_ns)

    review = sub.add_parser(
        "review", help="Inspect or bulk-approve prompt review candidates"
    )
    review_sub = review.add_subparsers(dest="review_command", required=True)

    p = review_sub.add_parser(
        "list", help="List pending candidates without displaying memory content"
    )
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default="default",
                   help="Workspace/namespace (default: default)")
    p.add_argument("--repo")
    p.add_argument("--source", action="append")
    p.add_argument("--legacy-agent-only", action="store_true")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_review_list)

    p = review_sub.add_parser(
        "approve", help="Approve a governed batch (dry-run unless --apply)"
    )
    p.add_argument("memory_ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--namespace", "--workspace", "-n", dest="namespace", default="default",
                   help="Workspace/namespace (default: default)")
    p.add_argument("--repo")
    p.add_argument("--source", action="append")
    p.add_argument("--legacy-agent-only", action="store_true")
    p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default=getpass.getuser())
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_review_approve)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValidationError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except _ServiceStartupError as exc:
        # Construction-phase failures keep the redacted, actionable
        # startup guidance (missing extra, bad database path, backend setup).
        print(f"Error: {_startup_error(exc.original)}")
        sys.exit(1)
    except (sqlite3.Error, OSError, ImportError, RuntimeError) as exc:
        # Command-phase failures of the same builtin types: the service is up,
        # so startup advice would mislead. Stay value-free — type name plus a
        # bare strerror never echoes the offending path or credential text.
        detail = getattr(exc, "strerror", "") or ""
        suffix = f": {detail}" if detail else ""
        print(f"Error: command failed ({type(exc).__name__}){suffix}.")
        sys.exit(1)


if __name__ == "__main__":
    main()
