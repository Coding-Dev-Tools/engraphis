"""Commercial control-plane configuration and release-readiness checks.

This module contains no customer data and never returns secret material.  It is shared by
the vendor-only ASGI app, billing webhook, release doctor, and tests so production gating
cannot drift between entrypoints.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from engraphis.config import SERVICE_MODES, settings


PRODUCT_ENV = {
    "POLAR_PRO_MONTHLY_PRODUCT_ID": ("pro", "monthly"),
    "POLAR_PRO_ANNUAL_PRODUCT_ID": ("pro", "annual"),
    "POLAR_TEAM_MONTHLY_PRODUCT_ID": ("team", "monthly"),
    "POLAR_TEAM_ANNUAL_PRODUCT_ID": ("team", "annual"),
}


def manifest() -> dict:
    path = Path(__file__).with_name("commercial_manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


def expected_product_ids() -> dict:
    expected = {}
    for plan_name in ("pro", "team"):
        for interval, product in manifest()["plans"][plan_name]["products"].items():
            expected[product["env"]] = {
                "id": product["id"], "plan": plan_name, "interval": interval}
    return expected


def service_mode() -> str:
    mode = (settings.service_mode or "").strip().lower()
    if mode not in SERVICE_MODES:
        raise RuntimeError(
            "ENGRAPHIS_SERVICE_MODE must be one of: %s" % ", ".join(SERVICE_MODES))
    return mode


def vendor_admin_token_ready() -> bool:
    """Require a bounded, high-entropy control-plane administrator credential."""
    token = os.environ.get("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "").strip()
    return 32 <= len(token) <= 4096 and all(
        char.isascii() and 33 <= ord(char) < 127 for char in token)


def product_catalog() -> dict:
    """Return exact configured Polar product ids without exposing any credential."""
    catalog = {}
    expected = expected_product_ids()
    for env_name, (plan, interval) in PRODUCT_ENV.items():
        product_id = os.environ.get(env_name, "").strip()
        if product_id and expected.get(env_name, {}).get("id") == product_id:
            catalog[product_id] = {"plan": plan, "interval": interval,
                                   "env": env_name}
    return catalog


def product_for_id(product_id: str) -> Optional[dict]:
    return product_catalog().get(str(product_id or "").strip())


def extract_product_id(data: dict) -> str:
    """Handle the Polar order/subscription shapes used by signed webhooks."""
    if not isinstance(data, dict):
        return ""
    product = data.get("product") or {}
    price = data.get("price") or {}
    candidates = [
        data.get("product_id"),
        product.get("id") if isinstance(product, dict) else product,
        price.get("product_id") if isinstance(price, dict) else "",
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value[:128]
    return ""


def _signer_matches() -> bool:
    try:
        from engraphis.inspector.webhooks import _load_signing_secret
        from engraphis.licensing import ed25519_public_key, vendor_public_key
        actual = ed25519_public_key(_load_signing_secret())
        return hmac.compare_digest(actual, vendor_public_key())
    except Exception:
        return False


def _registry_writable() -> bool:
    try:
        from engraphis.inspector import license_registry
        conn = license_registry.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("SELECT 1").fetchone()
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        return True
    except Exception:
        return False


def _disk_ok() -> bool:
    try:
        from engraphis.inspector import license_registry
        db_path = Path(license_registry._db_path()).expanduser().resolve()
        root = db_path.parent
        root.mkdir(parents=True, exist_ok=True)
        minimum = max(1, int(os.environ.get(
            "ENGRAPHIS_VENDOR_MIN_FREE_BYTES", str(256 * 1024 * 1024))))
        return shutil.disk_usage(root).free >= minimum
    except Exception:
        return False


def _customer_disk_ok() -> bool:
    try:
        root = Path(settings.db_path).expanduser().resolve().parent
        root.mkdir(parents=True, exist_ok=True)
        minimum = max(1, int(os.environ.get(
            "ENGRAPHIS_CUSTOMER_MIN_FREE_BYTES", str(256 * 1024 * 1024))))
        return shutil.disk_usage(root).free >= minimum
    except Exception:
        return False


def _backup_fresh() -> bool:
    """Validate the marker and encrypted artifact written by the backup job.

    A status file is only an attestation, not proof: recompute the artifact digest and
    require the file to live directly in the configured off-volume directory.  This
    prevents a copied/edited marker, a path traversal, or a same-size damaged archive
    from holding production readiness open.
    """
    marker = os.environ.get("ENGRAPHIS_BACKUP_STATUS_FILE", "").strip()
    output = os.environ.get("ENGRAPHIS_BACKUP_OUTPUT_DIR", "").strip()
    if not marker or not output:
        return False
    try:
        maximum = max(60, int(os.environ.get(
            "ENGRAPHIS_BACKUP_MAX_AGE_SECONDS", "93600")))  # 26 hours
        marker_source = Path(marker).expanduser()
        if marker_source.is_symlink():
            return False
        marker_path = marker_source.resolve(strict=True)
        if not marker_path.is_file():
            return False
        output_path = Path(output).expanduser().resolve(strict=True)
        if not output_path.is_dir():
            return False
        status = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(status, dict) \
                or status.get("schema") != "engraphis-backup-status/v1":
            return False
        created_at = float(status["created_at"])
        artifact_source = Path(str(status["artifact"])).expanduser()
        expected_size = int(status["bytes"])
        checksum = str(status["sha256"])
        if not artifact_source.is_absolute() or artifact_source.is_symlink() \
                or expected_size <= 0 \
                or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            return False
        now = time.time()
        age = now - created_at
        if not math.isfinite(created_at) or not -300 <= age <= maximum:
            return False
        artifact = artifact_source.resolve(strict=True)
        if artifact.parent != output_path or not artifact.is_file():
            return False
        artifact_stat = artifact.stat()
        marker_mtime = marker_path.stat().st_mtime
        if abs(marker_mtime - created_at) > 300 \
                or abs(artifact_stat.st_mtime - created_at) > 300 \
                or artifact_stat.st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with artifact.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return hmac.compare_digest(digest.hexdigest(), checksum)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def run_configured_backup() -> dict:
    """Create and verify one encrypted backup without exposing its path or key."""
    output = os.environ.get("ENGRAPHIS_BACKUP_OUTPUT_DIR", "").strip()
    marker = os.environ.get("ENGRAPHIS_BACKUP_STATUS_FILE", "").strip()
    if not output or not marker:
        raise RuntimeError("off-volume backup storage is not configured")
    try:
        retention = max(1, int(os.environ.get(
            "ENGRAPHIS_BACKUP_RETENTION_DAYS", "30")))
    except ValueError as exc:
        raise RuntimeError("backup retention is invalid") from exc
    from scripts.commercial_backup import backup
    try:
        artifact = backup(Path(output), Path(marker), retention, False)
    except SystemExit as exc:
        raise RuntimeError("backup configuration was rejected") from exc
    return {
        "ok": bool(artifact.is_file()),
        "verified": bool(artifact.is_file() and _backup_fresh()),
    }


def customer_operations_readiness() -> dict:
    """Secret-free managed-customer storage readiness for authenticated monitoring."""
    checks = {
        "service_mode": service_mode() == "customer",
        "disk": _customer_disk_ok(),
        "backup": _backup_fresh(),
    }
    checks["ready"] = all(checks.values())
    return checks


def vendor_serving_readiness() -> dict:
    """Dependencies required to safely receive live licensing and billing traffic.

    This intentionally excludes backup freshness and SLO/alert signals. An orchestrator
    draining a healthy process cannot repair those conditions and may instead deadlock a
    first deployment before the authenticated backup endpoint can run. The full
    operational gate remains :func:`vendor_readiness`.
    """
    from engraphis.billing import webhook_secret_ready, webhook_state_ready
    from engraphis.inspector.webhooks import email_configured
    from engraphis.licensing import VENDOR_SIGNER_RELEASE_READY

    products = product_catalog()
    checks = {
        "service_mode": service_mode() == "vendor",
        "signer": _signer_matches(),
        "signer_release_ready": bool(VENDOR_SIGNER_RELEASE_READY),
        "registry": _registry_writable(),
        "polar_webhook": webhook_secret_ready(),
        "polar_organization": bool(os.environ.get("POLAR_ORGANIZATION_ID", "").strip()),
        "polar_products": len(products) == len(PRODUCT_ENV),
        "polar_idempotency": webhook_state_ready(require_durable=True),
        "email": bool(email_configured()),
        "disk": _disk_ok(),
    }
    checks["ready"] = all(checks.values())
    return checks


def vendor_readiness() -> dict:
    """Return the full secret-free operational release gate."""
    from engraphis.billing import webhook_backlog_healthy
    from engraphis.email_outbox import health as email_outbox_health
    from engraphis.inspector.license_registry import rejected_lease_health
    from engraphis.inspector.webhooks import manual_fulfillment_clear
    from engraphis.resend_events import webhook_secret_ready

    checks = vendor_serving_readiness()
    checks.pop("ready", None)
    try:
        outbox_healthy = bool(email_outbox_health()["healthy"])
    except Exception:
        outbox_healthy = False
    checks.update({
        "vendor_admin_token": vendor_admin_token_ready(),
        "polar_backlog": webhook_backlog_healthy(),
        "rejected_leases": rejected_lease_health(),
        "email_webhook": webhook_secret_ready(),
        "email_outbox": outbox_healthy,
        "manual_fulfillment": manual_fulfillment_clear(),
        "backup": _backup_fresh(),
    })
    # Invalid public registration attempts are an operator alert, not a dependency.
    # Making this attacker-controlled signal a readiness gate lets anyone force the
    # orchestrator to drain/restart a healthy licensing service by submitting bad keys.
    checks["ready"] = all(
        value for name, value in checks.items() if name != "rejected_leases")
    return checks
