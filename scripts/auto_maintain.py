#!/usr/bin/env python3
"""Run the Engraphis automated-maintenance sweep (Pro feature: ``automation``).

Schedule this from Windows Task Scheduler or cron to keep the memory store clean
without the dashboard open. It honors the policy saved from the dashboard's
Automation view (see :mod:`engraphis.automation`).

    python -m scripts.auto_maintain            # dry-run preview, prints what WOULD change
    python -m scripts.auto_maintain --apply    # apply the policy (respects cadence)
    python -m scripts.auto_maintain --apply --force   # apply even if not yet due

Pro-gated: on the free tier it exits 0 with a one-line note (so a scheduled task
doesn't spam failures) and changes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Engraphis automated maintenance.")
    ap.add_argument("--apply", action="store_true",
                    help="Apply changes (default: dry-run preview only).")
    ap.add_argument("--force", action="store_true",
                    help="Run even if the cadence interval has not elapsed.")
    args = ap.parse_args()

    from engraphis import automation, licensing
    from engraphis.config import settings
    from engraphis.service import MemoryService

    if not licensing.has_feature("automation"):
        sys.stderr.write(
            "Automated maintenance is an Engraphis Pro feature. Start a free trial from "
            "the dashboard (Settings -> License), or set ENGRAPHIS_LICENSE_KEY.\n")
        return 0

    policy = automation.load_policy()
    if args.apply and not args.force and not automation.due(policy):
        print("Not due yet (cadence %sh). Use --force to run now."
              % policy.get("cadence_hours"))
        return 0

    svc = MemoryService.create(settings.db_path, embed_model=settings.embed_model,
                               embed_dim=settings.embed_dim or 256)
    result = automation.run_maintenance(svc, dry_run=not args.apply, policy=policy)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
