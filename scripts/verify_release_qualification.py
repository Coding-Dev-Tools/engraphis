"""Verify owner-signed full-product qualification before any release publication.

This public verifier contains no signing operation or private ledger reader. The
release owner supplies a content-free receipt and its trusted Ed25519 public key
through protected configuration; missing configuration is a publication failure.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from scripts.check_release_readiness import RELEASE_GATES
from scripts.release_evidence import distribution_artifacts, validate_commit, validate_tag


SCHEMA = "engraphis-release-qualification/v1"
_DOMAIN = (SCHEMA + "\n").encode("ascii")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_MAX_RECEIPT = 16 * 1024
_PAYLOAD_FIELDS = {
    "engine_commit", "distributions", "candidate_id", "ledger_sha256",
    "release_gates", "issued_at", "expires_at", "release_approved",
}


class QualificationError(ValueError):
    """The configured approval does not authorize these exact release bytes."""


def signing_bytes(payload: dict) -> bytes:
    """Public encoding contract only; this function never signs anything."""
    return _DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError("duplicate qualification JSON key")
        result[key] = value
    return result


def _reject_number(_):
    raise QualificationError("qualification does not contain numeric fields")


def _decode_receipt(raw: str) -> dict:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > _MAX_RECEIPT:
        raise QualificationError("qualification receipt is absent or exceeds its size limit")
    try:
        receipt = json.loads(raw, object_pairs_hook=_pairs, parse_float=_reject_number,
                             parse_int=_reject_number, parse_constant=_reject_number)
    except (ValueError, RecursionError) as exc:
        raise QualificationError("qualification receipt must be unambiguous JSON") from exc
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "payload", "signature"}
            or receipt.get("schema") != SCHEMA):
        raise QualificationError("unsupported qualification receipt")
    return receipt


def _base64(value: Any, size: int, label: str) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError
    except (ValueError, binascii.Error) as exc:
        raise QualificationError(label + " must use canonical base64") from exc
    return decoded


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise QualificationError(label + " must be a lowercase SHA-256 digest")
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise QualificationError("qualification times must be UTC ISO timestamps ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise QualificationError("qualification timestamp is invalid") from exc


def verify_qualification(
    raw_receipt: str, public_key: str, *, commit: str, tag: str,
    distribution_directory: Path, candidate_id: str, ledger_sha256: str,
    now: Optional[datetime] = None,
) -> dict:
    """Verify a signed approval against independently selected source and artifacts."""
    receipt = _decode_receipt(raw_receipt)
    key_bytes = _base64(public_key, 32, "qualification public key")
    signature = _base64(receipt["signature"], 64, "qualification signature")
    payload = receipt["payload"]
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
        raise QualificationError("qualification payload fields must match the public contract")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise QualificationError("Ed25519 verification requires the release cryptography dependency") from exc
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, signing_bytes(payload))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise QualificationError("qualification signature verification failed") from exc
    try:
        checked_commit = validate_commit(commit)
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError
        validate_tag(tag, tag[1:])
        distributions = {
            item["filename"]: item["sha256"]
            for item in distribution_artifacts(distribution_directory, tag[1:])
        }
    except (OSError, ValueError) as exc:
        raise QualificationError("release source, tag or distribution set is invalid") from exc
    if payload["engine_commit"] != checked_commit or payload["distributions"] != distributions:
        raise QualificationError("qualification does not match the exact commit and distribution bytes")
    if payload["candidate_id"] != _hash(candidate_id, "expected candidate ID"):
        raise QualificationError("qualification belongs to another full-product candidate")
    if payload["ledger_sha256"] != _hash(ledger_sha256, "expected private ledger digest"):
        raise QualificationError("qualification belongs to another private ledger")
    gates = payload["release_gates"]
    if (not isinstance(gates, dict) or set(gates) != set(RELEASE_GATES)
            or any(value != "PASS" for value in gates.values())):
        raise QualificationError("every mandatory release gate must explicitly be PASS")
    if payload["release_approved"] is not True:
        raise QualificationError("qualification requires explicit release approval")
    issued = _utc(payload["issued_at"])
    expires = _utc(payload["expires_at"])
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise QualificationError("verification clock must include a timezone")
    if expires <= issued or current < issued or current >= expires:
        raise QualificationError("qualification is not currently valid")
    return {
        "schema": "engraphis-release-qualification-check/v1", "status": "PASS",
        "engine_commit": checked_commit, "candidate_id": candidate_id,
        "ledger_sha256": ledger_sha256, "distributions": distributions,
        "receipt_sha256": hashlib.sha256(raw_receipt.encode("utf-8")).hexdigest(),
        "authority_sha256": hashlib.sha256(key_bytes).hexdigest(),
        "expires_at": payload["expires_at"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    try:
        verify_qualification(
            os.environ.get("ENGRAPHIS_RELEASE_QUALIFICATION", ""),
            os.environ.get("ENGRAPHIS_RELEASE_VERIFY_KEY", ""),
            commit=args.commit, tag=args.tag, distribution_directory=args.dist,
            candidate_id=os.environ.get("ENGRAPHIS_RELEASE_CANDIDATE_ID", ""),
            ledger_sha256=os.environ.get("ENGRAPHIS_RELEASE_LEDGER_SHA256", ""),
        )
    except (QualificationError, OSError) as exc:
        print("Release qualification blocked: " + str(exc), file=sys.stderr)
        return 1
    print("Full-product release qualification verified for the selected distribution bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
