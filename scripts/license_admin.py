"""Vendor-side license CLI — inventory, issuance, verification, and signer rotation.

This is YOUR tool, not the customer's. The private signing key never ships and never
belongs in the repo: ``keygen`` writes it to ``.secrets/`` (gitignored) and prints the
public half to pin in ``engraphis/licensing.py``.

    python -m scripts.license_admin keygen
    python -m scripts.license_admin inventory
    python -m scripts.license_admin issue --email a@b.co --plan team --seats 5 --days 365
    python -m scripts.license_admin verify ENGR1.xxxx.yyyy
    python -m scripts.license_admin rotation-reissue --help
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

from engraphis.licensing import (
    _DEV_VENDOR_PUBKEY_HEX, PLAN_FEATURES, LicenseError, compose_key, ed25519_public_key,
    parse_key, vendor_public_key,
)

_DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / ".secrets" / "vendor_signing.key"


def _write_private_file(path: Path, payload: str, *, overwrite: bool = False) -> None:
    """Durably write mode-0600 material without clobbering an existing path by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        sys.exit(f"{path} exists — choose a new path")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(str(temporary), str(path))
        else:
            try:
                os.link(str(temporary), str(path))
            except FileExistsError:
                sys.exit(f"{path} exists — choose a new path")
            temporary.unlink()
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _decode_verified_key(key: str):
    """Return the parsed license and its exact signed payload, accepting expired keys."""
    parsed = parse_key(key, now=0)
    encoded = key.split(".")[1]
    body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    payload = json.loads(body.decode("utf-8"))
    return parsed, payload


def _load_source_keys(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        sys.exit(f"could not read source-license file {path}: {exc}")
    keys = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not keys:
        sys.exit(f"{path} contains no license keys")
    return keys


def _same_number(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def _assert_registry_match(parsed, row: dict) -> None:
    """Reject a source manifest that disagrees with durable registry metadata."""
    expected = {
        "email": parsed.email,
        "plan": parsed.plan,
        "seats": parsed.seats,
    }
    for field, value in expected.items():
        if row.get(field) != value:
            sys.exit(f"source key {parsed.key_id} does not match registry field {field}")
    for field in ("issued", "expires"):
        if not _same_number(row.get(field), getattr(parsed, field)):
            sys.exit(f"source key {parsed.key_id} does not match registry field {field}")
    for field in ("subscription_id", "order_id"):
        stored = str(row.get(field) or "")
        if stored and stored != getattr(parsed, field):
            sys.exit(f"source key {parsed.key_id} does not match registry field {field}")
    stored_signer = str(row.get("signing_key_id") or "").strip().lower()
    if stored_signer and stored_signer != parsed.signing_key_id:
        sys.exit(f"source key {parsed.key_id} does not match its registered signer")


def _load_rotation_manifest(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        sys.exit(f"could not read rotation manifest {path}: {exc}")
    except ValueError:
        sys.exit(f"{path} is not valid JSON")
    if not isinstance(document, dict) \
            or document.get("schema") != "engraphis-signer-rotation/v1":
        sys.exit(f"{path} is not an Engraphis signer-rotation manifest")
    return document


def _manifest_projection(document: dict) -> dict:
    """Comparable, deterministic portion of a rotation manifest (excludes its timestamp)."""
    fields = (
        "schema", "source_key_ids", "source_signing_key_ids",
        "replacement_signing_key_id", "reissues",
    )
    return {field: document.get(field) for field in fields}


def _write_rotation_manifest(path: Path, document: dict, *, resume: bool) -> None:
    if path.exists():
        if not resume:
            sys.exit(f"{path} exists — use --resume only for the same interrupted rotation")
        existing = _load_rotation_manifest(path)
        if _manifest_projection(existing) != _manifest_projection(document):
            sys.exit(f"{path} does not match the requested rotation")
        return
    _write_private_file(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


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
        sys.exit(f"{path} exists — choose a new --key-file path (or deliberately use --force)")
    secret = secrets.token_bytes(32)
    _write_private_file(path, secret.hex() + "\n", overwrite=bool(args.force))
    pub = ed25519_public_key(secret).hex()
    print(f"private signing key  -> {path}  (KEEP OFF DEV BOXES; encrypted recovery backup)")
    print(f"public verify key    -> {pub}")
    print(f"public key id        -> {pub[:16]}")
    print("pin it: set _VENDOR_PUBKEY_HEX in engraphis/licensing.py to the value above")


def cmd_issue(args) -> None:
    if args.plan not in PLAN_FEATURES:
        sys.exit(f"plan must be one of: {', '.join(sorted(PLAN_FEATURES))}")
    secret = _load_secret(Path(args.key_file))
    if ed25519_public_key(secret).hex() == _DEV_VENDOR_PUBKEY_HEX:
        print(
            "WARNING: signing with the known-compromised DEV keypair (its private seed has "
            "been on dev boxes / in agent sessions). Anyone holding that seed can forge "
            "identical keys — DO NOT SELL keys signed with it. "
            "Rotate first: `python -m scripts.license_admin keygen "
            "--key-file <new-secure-path>`. "
            "Proceeding (fine for local testing).",
            file=sys.stderr)
    now = time.time()
    public = ed25519_public_key(secret).hex()
    payload = {
        "v": 1, "plan": args.plan, "email": args.email,
        "seats": max(1, args.seats), "issued": int(now),
        "expires": int(now + args.days * 86400) if args.days else None,
        "signing_key_id": public[:16],
    }
    if args.feature:
        payload["features"] = sorted(set(args.feature))
    if args.trial:
        payload["trial"] = 1
    # Online-only by default: bake in the signed cloud-enforcement claim so the key needs a
    # live server lease (and is remotely revocable). --cloud-url overrides the server;
    # --offline omits the claim (VENDOR TESTING ONLY — such a key verifies by signature
    # alone, works offline, and can NOT be revoked; never sell one).
    if not args.offline:
        from engraphis.config import resolve_license_server_url
        cloud = resolve_license_server_url(args.cloud_url)
        if cloud:
            payload["enforce"] = "cloud"
            payload["cloud_url"] = cloud
    key = compose_key(payload, secret)
    from engraphis.inspector.license_registry import record_issued
    record_issued(key)
    print(key)
    if args.json:
        print(json.dumps(payload, indent=2), file=sys.stderr)


def cmd_verify(args) -> None:
    try:
        lic = parse_key(args.key)
    except LicenseError as exc:
        sys.exit(f"INVALID: {exc}")
    print(json.dumps(lic.to_public_dict(), indent=2))


def cmd_inventory(args) -> None:
    from engraphis.inspector.license_registry import inventory
    print(json.dumps(inventory(args.db_path or None), indent=2, sort_keys=True))


def _build_rotation(args):
    from engraphis.inspector.license_registry import signer_rotation_state

    secret = _load_secret(Path(args.new_key_file))
    replacement_public = ed25519_public_key(secret).hex()
    replacement_signer = replacement_public[:16]
    if replacement_public == _DEV_VENDOR_PUBKEY_HEX:
        sys.exit("refusing to rotate to the known-compromised development signer")
    if replacement_public != vendor_public_key().hex():
        sys.exit(
            "new signing key does not match the pinned current vendor public key; "
            "ship verifier compatibility first")

    sources = {}
    payloads = {}
    for key in _load_source_keys(Path(args.source_file)):
        try:
            parsed, payload = _decode_verified_key(key)
        except LicenseError as exc:
            sys.exit(f"source-license file contains an invalid key: {exc}")
        if parsed.key_id in sources:
            sys.exit(f"duplicate source license fingerprint: {parsed.key_id}")
        sources[parsed.key_id] = (key, parsed)
        payloads[parsed.key_id] = payload

    state = signer_rotation_state(
        replacement_signer, db_path=args.db_path or None)
    candidates = {str(row["key_id"]): row for row in state["candidates"]}
    completed = {str(row["source_key_id"]): row for row in state["completed"]}
    expected_ids = set(candidates) | set(completed)
    supplied_ids = set(sources)
    missing = sorted(expected_ids - supplied_ids)
    extra = sorted(supplied_ids - expected_ids)
    if missing:
        sys.exit("source-license file is missing registry key ids: " + ", ".join(missing))
    if extra:
        sys.exit("source-license file contains non-candidate key ids: " + ", ".join(extra))

    reissues = []
    already_current = 0
    plans = {}
    for source_id in sorted(sources):
        _source_key, parsed = sources[source_id]
        plans[parsed.plan] = plans.get(parsed.plan, 0) + 1
        if source_id in candidates:
            _assert_registry_match(parsed, candidates[source_id])
        if source_id in completed:
            audit = completed[source_id]
            if audit["source_signing_key_id"] != parsed.signing_key_id:
                sys.exit(f"rotation audit signer mismatch for source key {source_id}")
        if parsed.signing_key_id == replacement_signer:
            already_current += 1
            continue

        replacement_payload = dict(payloads[source_id])
        replacement_payload["signing_key_id"] = replacement_signer
        replacement_key = compose_key(replacement_payload, secret)
        replacement = parse_key(replacement_key, now=0)
        if source_id in completed \
                and completed[source_id]["replacement_key_id"] != replacement.key_id:
            sys.exit(f"rotation audit replacement mismatch for source key {source_id}")
        reissues.append({
            "source_key_id": source_id,
            "source_signing_key_id": parsed.signing_key_id,
            "replacement_key_id": replacement.key_id,
            "email": parsed.email,
            "plan": parsed.plan,
            "expires": replacement_payload.get("expires"),
            "subscription_id": parsed.subscription_id,
            "order_id": parsed.order_id,
            "license_key": replacement_key,
        })

    document = {
        "schema": "engraphis-signer-rotation/v1",
        "created_at": int(time.time()),
        "source_key_ids": sorted(sources),
        "source_signing_key_ids": sorted({
            parsed.signing_key_id for _key, parsed in sources.values()
        }),
        "replacement_signing_key_id": replacement_signer,
        "reissues": reissues,
    }
    summary = {
        "applied": False,
        "source_keys": len(sources),
        "source_signing_key_ids": document["source_signing_key_ids"],
        "replacement_signing_key_id": replacement_signer,
        "reissued": len(reissues),
        "already_on_replacement_signer": already_current,
        "plans": plans,
    }
    return document, summary


def cmd_rotation_reissue(args) -> None:
    from engraphis.inspector.license_registry import record_signer_rotation

    document, summary = _build_rotation(args)
    if not args.apply:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    output = Path(args.output_file)
    protected_inputs = {
        Path(args.source_file).resolve(),
        Path(args.new_key_file).resolve(),
    }
    if output.resolve() in protected_inputs:
        sys.exit("rotation output must not overwrite a source-license or signing-key file")
    _write_rotation_manifest(output, document, resume=bool(args.resume))
    tuples = [
        (
            str(item["source_key_id"]),
            str(item["source_signing_key_id"]),
            str(item["license_key"]),
        )
        for item in document["reissues"]
    ]
    try:
        inserted = record_signer_rotation(
            tuples,
            replacement_signing_key_id=str(document["replacement_signing_key_id"]),
            db_path=args.db_path or None,
        )
    except (LicenseError, ValueError) as exc:
        sys.exit(
            f"rotation manifest was written but registry update failed: {exc}; "
            "fix the registry and rerun with --resume")
    summary.update({
        "applied": True,
        "manifest": str(output),
        "registry_reissues_recorded": inserted,
    })
    print(json.dumps(summary, indent=2, sort_keys=True))


def cmd_rotation_retire(args) -> None:
    from engraphis.inspector.license_registry import retire_signer_rotation_sources

    document = _load_rotation_manifest(Path(args.manifest_file))
    reissues = document.get("reissues")
    target = str(document.get("replacement_signing_key_id") or "")
    if not isinstance(reissues, list) or not reissues:
        sys.exit("rotation manifest contains no source keys to retire")
    if any(not isinstance(item, dict) or not str(item.get("source_key_id") or "").strip()
           for item in reissues):
        sys.exit("rotation manifest contains an invalid source-key entry")
    source_ids = [str(item["source_key_id"]).strip() for item in reissues]
    if len(set(source_ids)) != len(source_ids):
        sys.exit("rotation manifest contains duplicate source-key entries")
    summary = {
        "applied": False,
        "replacement_signing_key_id": target,
        "source_keys": len(source_ids),
        "minimum_grace_days": 30,
    }
    if not args.apply:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    if not args.confirm_activated:
        sys.exit("--confirm-activated is required after every replacement is delivered")
    try:
        revoked = retire_signer_rotation_sources(
            source_ids,
            replacement_signing_key_id=target,
            db_path=args.db_path or None,
        )
    except ValueError as exc:
        sys.exit(f"rotation sources were not retired: {exc}")
    summary.update({"applied": True, "revoked": revoked})
    print(json.dumps(summary, indent=2, sort_keys=True))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="license_admin", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser(
        "inventory", help="show PII-free issued-key counts before signer rotation")
    inv.add_argument("--db-path", default="", help="registry database override")
    inv.set_defaults(fn=cmd_inventory)

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
    iss.add_argument("--cloud-url", default="",
                     help="license-server URL to bake into the key "
                          "(default: https://license.engraphis.com); ignored with --offline")
    iss.add_argument("--offline", action="store_true",
                     help="omit cloud enforcement (VENDOR TESTING ONLY — the key verifies "
                          "by signature alone and is NOT server-revocable)")
    iss.add_argument("--trial", action="store_true",
                     help="mark as a trial key (sets the signed trial flag)")
    iss.add_argument("--json", action="store_true", help="echo payload to stderr")
    iss.set_defaults(fn=cmd_issue)

    rotate = sub.add_parser(
        "rotation-reissue",
        help="preserve and re-sign every active registry license during signer rotation")
    rotate.add_argument(
        "--source-file", required=True,
        help="private file containing one existing license key per line")
    rotate.add_argument("--new-key-file", required=True, help="new private signing seed file")
    rotate.add_argument(
        "--output-file", required=True,
        help="new mode-0600 JSON manifest for controlled replacement delivery")
    rotate.add_argument("--db-path", default="", help="registry database override")
    rotate.add_argument(
        "--apply", action="store_true",
        help="write the replacement manifest and atomically register replacements")
    rotate.add_argument(
        "--resume", action="store_true",
        help="reuse an identical manifest left by an interrupted --apply")
    rotate.set_defaults(fn=cmd_rotation_reissue)

    retire = sub.add_parser(
        "rotation-retire",
        help="revoke audited source keys after replacement activation and 30-day grace")
    retire.add_argument("--manifest-file", required=True)
    retire.add_argument("--db-path", default="", help="registry database override")
    retire.add_argument("--confirm-activated", action="store_true")
    retire.add_argument("--apply", action="store_true")
    retire.set_defaults(fn=cmd_rotation_retire)

    ver = sub.add_parser("verify", help="verify a key against the pinned public key")
    ver.add_argument("key")
    ver.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
