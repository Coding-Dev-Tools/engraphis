#!/usr/bin/env python3
"""Cloud sync as a local command — your machine, your folder, your keys.

Point two or more devices at one shared folder (Dropbox / iCloud / OneDrive /
Syncthing / a network drive / a git repo) and sync your Engraphis memory store
across all of them, with deterministic conflict resolution — no "conflicted copy"
files, no lost notes. Examples::

    # Preview what a sync would change (recommended first run — never writes)
    python -m scripts.sync --db engraphis.db --workspace acme --remote "D:/Dropbox/engraphis" --dry-run

    # Sync for real: publish this device's snapshot, pull + merge every other device's
    python -m scripts.sync --db engraphis.db --workspace acme --remote "D:/Dropbox/engraphis"

Schedule it (cron)::      */15 * * * *  cd /path/to/repo && python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis
Schedule it (Windows)::   schtasks /Create /SC MINUTE /MO 15 /TN EngraphisSync /TR "python -m scripts.sync --db C:\\path\\engraphis.db --workspace acme --remote C:\\Users\\me\\Dropbox\\engraphis"

Shared-folder sync is a local transport. Managed-relay sync uses a scoped, expiring token;
the private cloud service is the sole authority for hosted entitlement and access.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlsplit

from engraphis.config import DEFAULT_RELAY_URL, settings
from engraphis.core.engine import MemoryEngine
from engraphis.core.sync import SyncEngine, SyncError
from engraphis.service import MemoryService


def _service(db_path: str) -> MemoryService:
    """Open the operational database through the configured application factory."""
    return MemoryService.create(
        db_path,
        embed_model=settings.embed_model or None,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim or 384,
        vector_backend=settings.vector_backend,
        rerank_model=getattr(settings, "rerank_model", "") or None,
        rerank_revision=getattr(settings, "rerank_revision", "") or None,
    )


def _relay_origin(value: object) -> str:
    """Return a comparison-only canonical origin without reflecting a supplied URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return ""
        # Force parsing of a malformed port before credentials are considered.
        _ = parsed.port
        return "%s://%s" % (parsed.scheme.lower(), parsed.netloc.lower())
    except ValueError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync an Engraphis workspace across devices.")
    ap.add_argument("--db", required=True, help="Path to the v2 database file.")
    ap.add_argument("--workspace", required=True, help="Workspace name to sync.")
    # Pick exactly one transport: a shared folder (self-hosted, free) or the managed relay.
    ap.add_argument("--remote", metavar="DIR",
                    help="Shared folder both devices can see (Dropbox/iCloud/Syncthing/…).")
    ap.add_argument("--relay", "--relay-url", dest="relay", nargs="?", const="",
                    metavar="URL",
                    help="Managed cloud relay root (e.g. https://relay.engraphis.com). "
                         "Bare --relay uses ENGRAPHIS_RELAY_URL. Mutually exclusive with --remote.")
    # Credentials and the long-lived workspace E2EE key must not appear in argv,
    # process listings, shell history, terminal scrollback, or exception reprs. Relay
    # credentials come from owner-only state/cloud session or the origin-bound
    # ENGRAPHIS_SYNC_TOKEN environment pair; the E2EE key comes from
    # ENGRAPHIS_SYNC_E2EE_KEY (normally injected by a secrets manager).
    ap.add_argument("--read-only", action="store_true",
                    help="Pull only; required for a viewer token without sync:write.")
    ap.add_argument("--repo", default=None, help="Restrict the sync to one repo name.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing (locally or to the remote).")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if any(
        item == flag or item.startswith(flag + "=")
        for item in raw_argv
        for flag in ("--relay-token", "--relay-e2ee-key")
    ):
        print(
            "error: relay secrets must be provided through owner-only state or "
            "the documented environment channels",
            file=sys.stderr,
        )
        return 2
    args = ap.parse_args(raw_argv)

    # Exactly one transport must be selected.
    use_relay = args.relay is not None
    if bool(args.remote) == use_relay:
        print("error: choose exactly one of --remote <folder> or --relay [<url>]",
              file=sys.stderr)
        return 2


    service = _service(args.db)
    try:
        return _sync(args, service.engine, use_relay=use_relay)
    finally:
        service.close()


def _sync(args: argparse.Namespace, engine: MemoryEngine, *,
          use_relay: bool) -> int:
    # Local folder sync needs no commercial authority. The managed relay checks its scoped
    # cloud token server-side for organization, workspace, expiry, scopes, and entitlement.
    from engraphis.backends.sync_relay import RelayError, has_sync_token, sync_read_only
    relay_token = None

    wid_row = engine.store.conn.execute(
        "SELECT id, settings FROM workspaces WHERE name=?", (args.workspace,)).fetchone()
    if not wid_row:
        print(f"error: no workspace named '{args.workspace}' in {args.db}", file=sys.stderr)
        return 2
    rid = None
    if args.repo:
        rid_row = engine.store.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND name=?",
            (wid_row["id"], args.repo)).fetchone()
        if not rid_row:
            print(f"error: no repo named '{args.repo}' in workspace '{args.workspace}'",
                  file=sys.stderr)
            return 2
        rid = rid_row["id"]

    from engraphis.backends.sync_folder import get_transport

    if use_relay:
        # Fail CLOSED before an off-device upload. This mirrors the service
        # authorization boundary: unreadable settings must never silently turn a
        # possibly-personal folder into a shared one.
        try:
            workspace_settings = json.loads(wid_row["settings"] or "{}")
        except (TypeError, ValueError, RecursionError):
            workspace_settings = None
        if not isinstance(workspace_settings, dict):
            print(
                "error: workspace settings are unreadable; refusing to upload to the "
                "shared-account relay (the folder could be marked personal)",
                file=sys.stderr,
            )
            return 2
        visibility = workspace_settings.get("visibility")
        if visibility == "personal":
            print(
                "error: personal workspaces are device-local and cannot be uploaded "
                "to the shared-account relay",
                file=sys.stderr,
            )
            return 2
        if visibility not in (None, "shared"):
            print(
                "error: workspace visibility is invalid; refusing to upload to the "
                "shared-account relay",
                file=sys.stderr,
            )
            return 2
        relay_url = args.relay or settings.relay_url
        if not relay_url:
            print("error: --relay needs a URL — pass --relay <url> or set ENGRAPHIS_RELAY_URL",
                  file=sys.stderr)
            return 2
        target_origin = _relay_origin(relay_url)
        canonical_origin = _relay_origin(DEFAULT_RELAY_URL)
        env_token = os.environ.get("ENGRAPHIS_SYNC_TOKEN")
        if env_token:
            configured_origin = _relay_origin(
                os.environ.get("ENGRAPHIS_SYNC_TOKEN_ORIGIN")
            )
            if not configured_origin or configured_origin != target_origin:
                print(
                    "error: configured relay credential is not bound to this relay origin",
                    file=sys.stderr,
                )
                return 2
            relay_token = env_token
        if not relay_token and not has_sync_token():
            if not target_origin or target_origin != canonical_origin:
                print(
                    "error: custom relay needs ENGRAPHIS_SYNC_TOKEN and "
                    "ENGRAPHIS_SYNC_TOKEN_ORIGIN",
                    file=sys.stderr,
                )
                return 2
            from engraphis.cloud_session import CloudSessionError, access_for_workspace
            try:
                relay_token, _, _ = access_for_workspace(
                    args.workspace, require_compute=False)
            except CloudSessionError as exc:
                print(f"error: cloud sync is not connected. {exc}", file=sys.stderr)
                return 2
        # Namespace the relay by workspace NAME (not the per-device local id) so every
        # device on the account lands in one bucket; account isolation is enforced
        # server-side by the scoped token owner through the hosted relay protocol.
        try:
            transport = get_transport(
                "relay",
                base_url=relay_url,
                workspace_id=args.workspace,
                access_token=relay_token,
                e2ee_key=os.environ.get("ENGRAPHIS_SYNC_E2EE_KEY"),
            )
        except (RelayError, ValueError) as exc:
            # A custom URL may contain credentials or signed query parameters. The
            # validator's fixed reason is actionable without reflecting the endpoint.
            print(f"error: could not open relay: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            # A dry run must not create an absent shared folder merely by opening its
            # transport.  FolderTransport treats a missing non-creating root as empty.
            transport = get_transport("folder", root=args.remote, create=not args.dry_run)
        except (ValueError, OSError) as exc:
            print(f"error: could not open sync folder '{args.remote}': {exc}", file=sys.stderr)
            return 2

    sync_device_id = None
    if args.dry_run:
        # SyncEngine normally mints and persists the database's stable device id at
        # construction. A preview must not perform even that local metadata write.
        from engraphis.core import ids
        sync_device_id = engine.store.get_sync_state("device_id") or ids.new_id("device")
    engine_sync = SyncEngine(engine.store, embedder=engine.embedder,
                             vector_index=engine.index, device_id=sync_device_id)
    # Honor the same durable, fail-closed device policy as dashboard auto-sync. This
    # matters for member/admin tokens too: a device explicitly configured download-only
    # must not silently regain upload authority merely because this CLI runs after a
    # process restart.
    read_only = bool(args.read_only or (use_relay and sync_read_only()))
    try:
        report = engine_sync.sync(
            transport,
            wid_row["id"],
            repo_id=rid,
            dry_run=args.dry_run,
            push=not read_only,
        )
    except (RelayError, SyncError, ValueError) as exc:
        print(f"error: relay sync failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))

    t = report["totals"]
    complete = report.get("complete") is not False
    if complete:
        verb = "would sync" if args.dry_run else "synced"
    else:
        verb = "would be incomplete" if args.dry_run else "incomplete"
    print(
        f"{verb}: {'read-only · ' if report.get('read_only') else ''}"
        f"exported {report['exported_memories']} memories · "
        f"pulled {report['peers_applied']} peer(s) · "
        f"+{t['added']} new, {t['updated']} updated, {t['unchanged']} unchanged, "
        f"+{t['links_added']} links"
        + (
            f" · {t['conflicts_preserved']} conflicts preserved"
            if t.get("conflicts_preserved") else ""
        )
        + (f" · {t['rejected']} rejected" if t.get("rejected") else ""),
        file=sys.stderr,
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
