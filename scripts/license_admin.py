"""Vendor-side license CLI — keygen / issue / verify (docs/LAUNCH_PLAN.md §2).

This is YOUR tool, not the customer's. The private signing key never ships and never
belongs in the repo: ``keygen`` writes it to ``.secrets/`` (gitignored) and prints the
public half to pin in ``engraphis/licensing.py``.

    python -m scripts.license_admin keygen
    python -m scripts.license_admin issue --email a@b.co --plan team --seats 5 --days 365
    python -m scripts.license_admin verify ENGR1.xxxx.yyyy
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

from engraphis.licensing import (
    PLAN_FEATURES, LicenseError, compose_key, ed25519_public_key, parse_key,
)

_DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / ".secrets" / "vendor_signing.key"


def _load_secret(path: Path) -> bytes:
    try:
        raw = bytes.fromhex(path.read_text(encoding="utf-8").strip())
    except OSError:
        sys.exit(f"no signing key at {path} — run `python -m scripts.license_admin keygen`")
    except ValueError:
        sys.exit(f"{path} is not valid hex")
    if len(raw) != 32:
        sys.exit(f"{path} must contain a 32-byte hex seed")
    return raw


def cmd_keygen(args) -> None:
    path = Path(args.key_file)
    if path.exists() and not args.force:
        sys.exit(f"{path} exists — pass --force to overwrite (this invalidates issued keys!)")
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.hex() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    pub = ed25519_public_key(secret).hex()
    print(f"private signing key  → {path}  (KEEP OFF DEV BOXES; password manager)")
    print(f"public verify key    → {pub}")
    print("pin it: set _VENDOR_PUBKEY_HEX in engraphis/licensing.py to the value above")


def cmd_issue(args) -> None:
    if args.plan not in PLAN_FEATURES:
        sys.exit(f"plan must be one of: {', '.join(sorted(PLAN_FEATURES))}")
    secret = _load_secret(Path(args.key_file))
    now = time.time()
    payload = {
        "v": 1, "plan": args.plan, "email": args.email,
        "seats": max(1, args.seats), "issued": int(now),
        "expires": int(now + args.days * 86400) if args.days else None,
    }
    if args.feature:
        payload["features"] = sorted(set(args.feature))
    key = compose_key(payload, secret)
    print(key)
    if args.json:
        print(json.dumps(payload, indent=2), file=sys.stderr)


def cmd_verify(args) -> None:
    try:
        lic = parse_key(args.key)
    except LicenseError as exc:
        sys.exit(f"INVALID: {exc}")
    print(json.dumps(lic.to_public_dict(), indent=2))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="license_admin", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    kg = sub.add_parser("keygen", help="generate a vendor signing keypair")
    kg.add_argument("--key-file", default=str(_DEFAULT_KEY_PATH))
    kg.add_argument("--force", action="store_true")
    kg.set_defaults(fn=cmd_keygen)

    iss = sub.add_parser("issue", help="issue a signed license key")
    iss.add_argument("--email", required=True)
    iss.add_argument("--plan", required=True, help="pro | team")
    iss.add_argument("--seats", type=int, default=1)
    iss.add_argument("--days", type=int, default=365,
                     help="validity in days; 0 = perpetual")
    iss.add_argument("--feature", action="append",
                     help="extra feature flag (repeatable)")
    iss.add_argument("--key-file", default=str(_DEFAULT_KEY_PATH))
    iss.add_argument("--json", action="store_true", help="echo payload to stderr")
    iss.set_defaults(fn=cmd_issue)

    ver = sub.add_parser("verify", help="verify a key against the pinned public key")
    ver.add_argument("key")
    ver.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
