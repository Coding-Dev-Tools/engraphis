"""Local, workspace-specific approval for readable managed processing.

This policy is private client state, not synced memory or the historical snapshot
consent marker. Missing/legacy state requires confirmation and permits no upload.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

SCHEMA = "engraphis-managed-processing/v1"


class ProcessingPolicyChanged(ValueError):
    """Another local policy update superseded an in-flight acknowledgement."""


def _key(workspace_id: str) -> str:
    return "managed_processing_policy:" + workspace_id


def processing_policy(service: Any, workspace: str) -> dict[str, Any]:
    ws = service._clean_ws(workspace)
    wid = service._lookup_workspace(ws)
    if not wid:
        raise ValueError("workspace does not exist")
    row = service.store.conn.execute(
        "SELECT value FROM sync_state WHERE key=?", (_key(wid),),
    ).fetchone()
    value: dict[str, Any] = {}
    if row:
        try:
            decoded = json.loads(row["value"])
            if isinstance(decoded, dict) and decoded.get("schema") == SCHEMA:
                value = decoded
        except (ValueError, TypeError, RecursionError):
            pass
    revision = value.get("revision", 0)
    remote_revision = value.get("remote_revision")
    valid_revision = isinstance(revision, int) and not isinstance(revision, bool) and revision > 0
    valid_remote = remote_revision is None or (
        isinstance(remote_revision, int) and not isinstance(remote_revision, bool) and remote_revision > 0
    )
    if not valid_revision or not valid_remote:
        value = {}
        revision, remote_revision = 0, None
    confirmed = value.get("confirmed") is True
    enabled = value.get("enabled") is True and confirmed
    override = os.environ.get("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "").strip().lower()
    operator_disabled = bool(override) and override not in {"1", "true", "yes", "on"}
    return {"workspace": ws, "workspace_id": wid, "schema": SCHEMA,
            "enabled": enabled and not operator_disabled, "confirmed": confirmed,
            "confirmation_required": not confirmed,
            "operator_disabled": operator_disabled,
            "revision": revision,
            "remote_revision": remote_revision,
            "remote_sync_pending": value.get("remote_sync_pending") is True,
            "updated_at": value.get("updated_at")}


def set_processing_policy(service: Any, workspace: str, *, enabled: bool,
                          confirmed: bool = False, remote_revision: Optional[int] = None,
                          remote_sync_pending: bool = False,
                          expected_revision: Optional[int] = None) -> dict[str, Any]:
    ws = service._clean_ws(workspace)
    service._authorize_workspace_control(ws)
    if not isinstance(enabled, bool) or not isinstance(confirmed, bool):
        raise ValueError("processing policy must use boolean values")
    if enabled and not confirmed:
        raise ValueError("managed processing requires explicit workspace confirmation")
    if remote_revision is not None and (
        isinstance(remote_revision, bool) or not isinstance(remote_revision, int)
        or remote_revision < 1
    ):
        raise ValueError("invalid remote processing policy revision")
    conn = service.store.conn
    owns = not conn.transaction_owned_by_current_thread()
    try:
        if owns:
            conn.execute("BEGIN IMMEDIATE")
        previous = processing_policy(service, ws)
        if expected_revision is not None and previous["revision"] != expected_revision:
            raise ProcessingPolicyChanged("Processing controls changed while Cloud was responding. Reload and retry.")
        value = {"schema": SCHEMA, "enabled": enabled, "confirmed": True,
                 "revision": int(previous["revision"]) + 1,
                 "remote_revision": remote_revision,
                 "remote_sync_pending": bool(remote_sync_pending), "updated_at": time.time()}
        conn.execute(
            "INSERT INTO sync_state(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (_key(previous["workspace_id"]), json.dumps(value, sort_keys=True), value["updated_at"]),
        )
        with conn.defer_commits():
            service.store.audit("user", "managed_processing_policy", previous["workspace_id"],
                                "enabled" if enabled else "disabled")
        if owns:
            conn.commit()
    except BaseException:
        if owns and conn.transaction_owned_by_current_thread():
            conn.rollback()
        raise
    return processing_policy(service, ws)
