"""engraphis connect - redeem the connect token from your account portal.

Your Engraphis account portal shows a one-time token and the exact command to run:

    engraphis connect --token engr_ct_...

That exchanges the token with the control plane and writes the owner-only session file
``~/.engraphis/cloud_session.json`` that the dashboard, MCP server, and Cloud Sync all
read.  Until this command existed the file had no writer at all, so a paying customer had
no supported way to connect a client.

    engraphis connect --token engr_ct_...            # the normal case
    engraphis connect --token -                      # read the token from stdin
    engraphis connect --token ... --workspace ws_1   # bind the device to one workspace
    engraphis connect --token ... --label "CI runner"
    engraphis connect --token ... --json             # redacted machine-readable summary

The token is a credential.  It is sent in the request body and nowhere else: it is never
printed, never logged, and never written to disk.  Passing it as an argument does put it
in your shell history, so ``--token -`` is available for scripted and shared machines.

Non-interactive by design (no prompts): safe in scripts, CI, and agent shells.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engraphis import cloud_session
from engraphis.device_connect import (
    DEFAULT_TIMEOUT_SECONDS,
    DeviceConnectError,
    connect,
)


def _prog(argv_zero: str) -> str:
    """Name this command the way the caller actually invoked it."""

    stem = Path(argv_zero or "").stem
    return stem if stem.endswith("connect") and stem != "connect" else "engraphis connect"


def _read_token(value: str) -> str:
    """Resolve ``--token -`` to one line of stdin, keeping it off the command line."""

    if value != "-":
        return value
    if sys.stdin is None or sys.stdin.isatty():
        # A bare `--token -` on a terminal would silently hang waiting for input.
        raise DeviceConnectError(
            "--token - reads the token from stdin; pipe it in, for example "
            "`printf %s \"$ENGRAPHIS_CONNECT_TOKEN\" | engraphis connect --token -`.",
            status=400,
        )
    return sys.stdin.readline()


def _print_summary(summary: dict) -> None:
    rows = [
        ("organization", summary.get("organization_id")),
        ("installation", summary.get("installation_id")),
        ("device", summary.get("device_id")),
        ("member", summary.get("member_id")),
        ("workspace", summary.get("workspace_id")),
        ("subject", summary.get("token_subject")),
    ]
    plan = str(summary.get("plan") or "").strip()
    if plan:
        active = summary.get("cloud_access_active")
        rows.append(("plan", plan + ("" if active is None else
                                     " (active)" if active else " (inactive)")))
    features = summary.get("cloud_features")
    if isinstance(features, (list, tuple)) and features:
        rows.append(("features", ", ".join(str(item) for item in features)))
    rows.append(("control url", summary.get("control_url")))
    rows.append(("compute url", summary.get("compute_url") or "(not configured)"))
    rows.append(("session file", summary.get("session_path")))

    print("Connected this device to Engraphis Cloud.")
    print()
    for label, value in rows:
        text = str(value or "").strip()
        if text:
            print("  %-14s %s" % (label, text))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog=_prog(sys.argv[0] if sys.argv else ""),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--token", required=True, metavar="TOKEN",
                    help="the connect token from your account portal, or - for stdin")
    ap.add_argument("--control-url", default=None, metavar="URL",
                    help="control plane to connect to (default: the shipped endpoint, "
                         "or ENGRAPHIS_CLOUD_CONTROL_URL)")
    ap.add_argument("--compute-url", default=None, metavar="URL",
                    help="managed compute endpoint (default: ENGRAPHIS_CLOUD_COMPUTE_URL, "
                         "or the shipped endpoint)")
    ap.add_argument("--workspace", default=None, metavar="WORKSPACE_ID",
                    help="bind this device to a single workspace")
    ap.add_argument("--label", default=None, metavar="TEXT",
                    help="label for this installation in your account portal")
    ap.add_argument("--device-name", default=None, metavar="TEXT",
                    help="device name shown in your account portal (default: hostname)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                    metavar="SECONDS", help="network timeout (default: %(default)s)")
    ap.add_argument("--json", action="store_true",
                    help="print the redacted summary as JSON instead of a report")
    args = ap.parse_args(argv)

    try:
        summary = connect(
            _read_token(args.token),
            control_url=args.control_url,
            compute_url=args.compute_url,
            workspace_id=args.workspace,
            installation_label=args.label,
            device_name=args.device_name,
            timeout=args.timeout,
        )
    except DeviceConnectError as exc:
        # ``str(exc)`` is fixed public copy from device_connect; it never carries the
        # token, the response body, or an internal hostname.
        print("%s: %s" % (ap.prog, exc), file=sys.stderr)
        return 1

    # Prove the write actually produced a session the rest of the client will use, rather
    # than reporting success on a file nothing can load.
    #
    # This runs *before* any success output is emitted, and that ordering is load-bearing.
    # ``configured()`` reads the session back and can raise -- an invalid
    # ``ENGRAPHIS_CLOUD_TOKEN_SUBJECT``, or the file changing under the read. Printing
    # first meant a complete ``--json`` success object was already on stdout when the
    # command then wrote an error and exited 1, so a consumer parsing stdout accepted a
    # connect that had failed, and a human got two contradictory answers.
    try:
        usable = cloud_session.configured()
    except cloud_session.CloudSessionError as exc:
        print("%s: the session was written but is not usable: %s" % (ap.prog, exc),
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, sort_keys=True, indent=2))
    else:
        _print_summary(summary)
        print()
        if usable:
            print("Next steps:")
            print("  engraphis-dashboard      # your plan's features are now unlocked")
            print("  engraphis-init --check   # verify the installation")
        else:
            print("Note: hosted compute is not configured yet, so managed compute "
                  "features stay off.")
            print("  Set ENGRAPHIS_CLOUD_COMPUTE_URL (or rerun with --compute-url) "
                  "using the endpoint")
            print("  shown in your account portal. Everything else is connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
