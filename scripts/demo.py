#!/usr/bin/env python3
"""Seed a disposable demo workspace and launch the local dashboard.

    python -m scripts.demo
    python -m scripts.demo --no-open --port 9000

The engraphis-demo console entry point is a separate packaging change.
The demo uses a fresh SQLite database inside a temporary directory. It never
reads or writes the database selected by ENGRAPHIS_DB_PATH. The offline
embedder is used for both the seed and the dashboard.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from scripts.start_dashboard import _port, _port_is_available

DEMO_FACTS = (
    {
        "content": "Checkout API uses JWT access tokens.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Legacy checkout token format",
        "subject_key": "auth.token_format",
        "claim_kind": "decision",
        "importance": 0.7,
    },
    {
        "content": "Checkout API uses PASETO access tokens.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout token format",
        "subject_key": "auth.token_format",
        "claim_kind": "decision",
        "importance": 0.9,
    },
    {
        "content": "The November auth migration replaced JWT with PASETO to retire the legacy signer.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Auth migration reason",
        "subject_key": "auth.migration_reason",
        "claim_kind": "decision",
        "importance": 0.8,
    },
    {
        "content": "Checkout frontend packages use pnpm. The lockfile is pnpm-lock.yaml; do not add a package-lock.json.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout package manager",
        "subject_key": "repo.package_manager",
        "claim_kind": "convention",
        "importance": 0.8,
    },
    {
        "content": "Run `pnpm test --filter checkout` before merging changes that touch the checkout package.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout test command",
        "subject_key": "repo.test_command",
        "claim_kind": "convention",
        "importance": 0.7,
    },
    {
        "content": "The checkout retry loop must send the same idempotency key on every attempt. A missing key caused duplicate charges in the October incident.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout retry idempotency",
        "subject_key": "checkout.idempotency",
        "claim_kind": "incident",
        "importance": 0.9,
    },
    {
        "content": "Order events are written to the outbox table in the same transaction as the order. The publisher worker drains the outbox.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout event delivery",
        "subject_key": "checkout.event_delivery",
        "claim_kind": "architecture",
        "importance": 0.8,
    },
    {
        "content": "Schema migrations require a verified backup and a rollback note before the migration runs.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Migration policy",
        "subject_key": "db.migration_policy",
        "claim_kind": "convention",
        "importance": 0.8,
    },
    {
        "content": "Stripe webhook handlers must be idempotent by event id. Store the processed event id before applying the change.",
        "workspace": "acme-api",
        "repo": "billing",
        "title": "Billing webhook idempotency",
        "subject_key": "billing.webhook_idempotency",
        "claim_kind": "convention",
        "importance": 0.9,
    },
    {
        "content": "The invoice reconciliation job double-counted prorations when a subscription changed mid-cycle. The fix reads the proration line items once and keys the result by invoice id.",
        "workspace": "acme-api",
        "repo": "billing",
        "title": "Proration reconciliation bug",
        "subject_key": "billing.proration_reconciliation",
        "claim_kind": "incident",
        "importance": 0.8,
    },
    {
        "content": "Dashboard components are server components by default. Add `use client` only at interactive leaves.",
        "workspace": "acme-web",
        "repo": "dashboard",
        "title": "Dashboard component default",
        "subject_key": "web.component_default",
        "claim_kind": "convention",
        "importance": 0.8,
    },
    {
        "content": "Dashboard feature flags are read at request time, not baked in at build time.",
        "workspace": "acme-web",
        "repo": "dashboard",
        "title": "Dashboard feature flags",
        "subject_key": "web.feature_flags",
        "claim_kind": "decision",
        "importance": 0.7,
    },
    {
        "content": "Use the design tokens in `styles/tokens.css`. Do not hardcode hex colors in dashboard components.",
        "workspace": "acme-web",
        "repo": "dashboard",
        "title": "Dashboard styling tokens",
        "subject_key": "web.styling",
        "claim_kind": "convention",
        "importance": 0.7,
    },
)


def seed_demo(service) -> list[dict]:
    """Write the demo facts through the public MemoryService API."""
    return [service.remember(**fact) for fact in DEMO_FACTS]


def create_demo_database(db_path: str | Path) -> tuple[list[dict], dict]:
    """Seed a new demo database and return its write results and sample recall."""
    path = Path(db_path).expanduser().resolve()
    if path.exists() and path.stat().st_size > 0:
        raise SystemExit(
            f"Refusing to overwrite the existing database at {path}. Choose a new demo path."
        )
    for suffix in ("-wal", "-shm"):
        companion = Path(str(path) + suffix)
        if companion.exists():
            raise SystemExit(f"Refusing to use {path}: the companion file {companion} exists.")
    path.parent.mkdir(parents=True, exist_ok=True)

    previous_embed = os.environ.get("ENGRAPHIS_EMBED_MODEL")
    os.environ["ENGRAPHIS_EMBED_MODEL"] = ""

    service = None
    try:
        from engraphis.service import MemoryService

        service = MemoryService.create(
            str(path),
            embed_model="",
            extractor="none",
            graph_extractor="regex",
            retention_supervisor="none",
        )
        results = seed_demo(service)
        sample = service.recall(
            "What token format does the checkout API use?",
            workspace="acme-api",
            repo="checkout",
            k=3,
        )
    finally:
        if service is not None:
            service.close()
        if previous_embed is None:
            os.environ.pop("ENGRAPHIS_EMBED_MODEL", None)
        else:
            os.environ["ENGRAPHIS_EMBED_MODEL"] = previous_embed
    return results, sample


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Seed a disposable Engraphis demo workspace and start the dashboard.",
    )
    ap.add_argument(
        "--host",
        default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"),
        help="Bind host (default: $ENGRAPHIS_HOST, else 127.0.0.1).",
    )
    ap.add_argument(
        "--port",
        type=_port,
        default=(os.environ.get("PORT") or os.environ.get("ENGRAPHIS_PORT", "8700")),
        help="Bind port (default: $PORT, else $ENGRAPHIS_PORT, else 8700).",
    )
    ap.add_argument("--no-open", action="store_true", help="Do not open the browser.")
    args = ap.parse_args(argv)

    if not _port_is_available(args.host, args.port):
        ap.exit(
            1,
            f"Port {args.port} is already in use. Choose another with --port; "
            "the demo never reuses an existing dashboard.\n",
        )

    with tempfile.TemporaryDirectory(prefix="engraphis-demo-") as demo_dir:
        db_path = Path(demo_dir) / "demo.db"
        print(
            "Creating a disposable demo database. Your existing Engraphis database is not modified."
        )
        print(
            "If an empty-database warning appears, it refers to the new demo file, not your real database."
        )
        results, sample = create_demo_database(db_path)

        os.environ["ENGRAPHIS_DB_PATH"] = str(db_path)
        os.environ["ENGRAPHIS_EMBED_MODEL"] = ""
        from engraphis import config as _config

        _config.settings.db_path = str(db_path)
        _config.settings.embed_model = ""
        if Path(_config.settings.db_path).resolve() != db_path:
            raise SystemExit(
                "Refusing to start: the dashboard settings did not bind to the demo database."
            )

        superseded = sum(len(result.get("superseded") or []) for result in results)
        print("Engraphis demo workspace")
        print(f"  Database   : {db_path}")
        print(f"  Memories   : {len(results)}")
        print(f"  Superseded : {superseded}")
        print("  Sample recall:")
        for line in str(sample.get("context") or "").splitlines():
            print(f"    {line}")
        print()

        from scripts.start_dashboard import main as dashboard_main

        forwarded = ["--host", args.host, "--port", str(args.port)]
        if args.no_open:
            forwarded.append("--no-open")
        dashboard_main(forwarded)


if __name__ == "__main__":
    main()
