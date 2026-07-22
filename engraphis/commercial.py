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
import tempfile
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

_BACKUP_STATUS_LOCAL = "engraphis-backup-status/v1"
_BACKUP_STATUS_S3 = "engraphis-backup-status/v2"
_BACKUP_S3_ENV = {
    "bucket": "ENGRAPHIS_BACKUP_S3_BUCKET",
    "endpoint": "ENGRAPHIS_BACKUP_S3_ENDPOINT",
    "access_key": "ENGRAPHIS_BACKUP_S3_ACCESS_KEY_ID",
    "secret_key": "ENGRAPHIS_BACKUP_S3_SECRET_ACCESS_KEY",
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


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_s3_config(*, required: bool = False) -> Optional[dict]:
    values = {
        name: os.environ.get(env_name, "").strip()
        for name, env_name in _BACKUP_S3_ENV.items()
    }
    configured = any(values.values())
    if not configured:
        if required:
            raise RuntimeError("backup object storage is not configured")
        return None
    missing = [env_name for name, env_name in _BACKUP_S3_ENV.items() if not values[name]]
    if missing:
        raise RuntimeError("backup object storage configuration is incomplete")
    if not values["endpoint"].startswith("https://"):
        raise RuntimeError("backup object storage endpoint must use HTTPS")
    prefix = os.environ.get("ENGRAPHIS_BACKUP_S3_PREFIX", "engraphis").strip().strip("/")
    if not prefix or any(part in {".", ".."} for part in prefix.split("/")):
        raise RuntimeError("backup object storage prefix is invalid")
    values["prefix"] = prefix
    values["region"] = os.environ.get("ENGRAPHIS_BACKUP_S3_REGION", "auto").strip() or "auto"
    return values


def _backup_s3_client(config: dict):
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - production dependency guard
        raise RuntimeError("backup object storage requires boto3") from exc
    return boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        region_name=config["region"],
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
    )


def _read_s3_digest(client, bucket: str, key: str) -> tuple[int, str]:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return size, digest.hexdigest()


def _atomic_backup_status(marker: Path, status: dict) -> None:
    marker = marker.expanduser()
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.is_symlink():
        raise RuntimeError("backup status marker must not be a symlink")
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, marker)
    completed_at = float(status["created_at"])
    os.utime(marker, (completed_at, completed_at))


def _s3_backup_fresh(status: dict, marker_path: Path, maximum: int) -> bool:
    config = _backup_s3_config(required=True)
    assert config is not None
    created_at = float(status["created_at"])
    expected_size = int(status["bytes"])
    checksum = str(status["sha256"])
    bucket = str(status["bucket"])
    key = str(status["key"])
    if bucket != config["bucket"] or not key.startswith(config["prefix"] + "/") \
            or expected_size <= 0 or len(checksum) != 64 \
            or any(char not in "0123456789abcdef" for char in checksum):
        return False
    age = time.time() - created_at
    if not math.isfinite(created_at) or not -300 <= age <= maximum \
            or abs(marker_path.stat().st_mtime - created_at) > 300:
        return False
    size, digest = _read_s3_digest(
        _backup_s3_client(config), config["bucket"], key)
    return size == expected_size and hmac.compare_digest(digest, checksum)


def _backup_fresh() -> bool:
    """Validate the marker and encrypted artifact written by the backup job.

    A status file is only an attestation, not proof: recompute the artifact digest and
    require the file to live directly in the configured off-volume directory.  This
    prevents a copied/edited marker, a path traversal, or a same-size damaged archive
    from holding production readiness open.
    """
    marker = os.environ.get("ENGRAPHIS_BACKUP_STATUS_FILE", "").strip()
    if not marker:
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
        status = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            return False
        if status.get("schema") == _BACKUP_STATUS_S3:
            return _s3_backup_fresh(status, marker_path, maximum)
        if status.get("schema") != _BACKUP_STATUS_LOCAL:
            return False
        output = os.environ.get("ENGRAPHIS_BACKUP_OUTPUT_DIR", "").strip()
        if not output:
            return False
        output_path = Path(output).expanduser().resolve(strict=True)
        if not output_path.is_dir():
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
        return hmac.compare_digest(_sha256_path(artifact), checksum)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _run_s3_backup(config: dict, marker: Path, retention: int) -> bool:
    from scripts.commercial_backup import PREFIX, backup

    with tempfile.TemporaryDirectory(prefix="engraphis-s3-backup-") as temporary:
        temporary_path = Path(temporary)
        try:
            artifact = backup(
                temporary_path / "artifacts",
                temporary_path / "local-status.json",
                retention,
                True,
            )
        except SystemExit as exc:
            raise RuntimeError("backup configuration was rejected") from exc
        checksum = _sha256_path(artifact)
        expected_size = artifact.stat().st_size
        key = "%s/%s" % (config["prefix"], artifact.name)
        client = _backup_s3_client(config)
        with artifact.open("rb") as fh:
            client.put_object(
                Bucket=config["bucket"],
                Key=key,
                Body=fh,
                ContentType="application/octet-stream",
                Metadata={"sha256": checksum},
            )
        remote_size, remote_digest = _read_s3_digest(client, config["bucket"], key)
        if remote_size != expected_size or not hmac.compare_digest(remote_digest, checksum):
            raise RuntimeError("uploaded backup verification failed")
        completed_at = time.time()
        _atomic_backup_status(marker, {
            "schema": _BACKUP_STATUS_S3,
            "bucket": config["bucket"],
            "key": key,
            "bytes": expected_size,
            "created_at": completed_at,
            "sha256": checksum,
        })
        cutoff = completed_at - retention * 86400
        response = client.list_objects_v2(
            Bucket=config["bucket"], Prefix=config["prefix"] + "/" + PREFIX)
        stale = []
        for item in response.get("Contents", []):
            modified = item.get("LastModified")
            timestamp = modified.timestamp() if hasattr(modified, "timestamp") else 0
            candidate = str(item.get("Key", ""))
            if candidate != key and candidate.startswith(config["prefix"] + "/") \
                    and timestamp and timestamp < cutoff:
                stale.append({"Key": candidate})
        if stale:
            client.delete_objects(Bucket=config["bucket"], Delete={"Objects": stale})
    return _backup_fresh()


def run_configured_backup() -> dict:
    """Create and verify one encrypted backup without exposing its path or key."""
    output = os.environ.get("ENGRAPHIS_BACKUP_OUTPUT_DIR", "").strip()
    marker = os.environ.get("ENGRAPHIS_BACKUP_STATUS_FILE", "").strip()
    if not marker:
        raise RuntimeError("off-volume backup storage is not configured")
    try:
        retention = max(1, int(os.environ.get(
            "ENGRAPHIS_BACKUP_RETENTION_DAYS", "30")))
    except ValueError as exc:
        raise RuntimeError("backup retention is invalid") from exc
    s3_config = _backup_s3_config()
    if s3_config is not None:
        verified = _run_s3_backup(s3_config, Path(marker), retention)
        return {"ok": verified, "verified": verified}
    if not output:
        raise RuntimeError("off-volume backup storage is not configured")
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
