"""Automatic cloud sync — the "set it and forget it" layer over the manual Sync button.

Cloud sync (``core/sync.py`` + the relay transport) already replicates the memory store
across a user's devices and, on Team, across the whole group. By default a human presses
"Sync now" in the dashboard (or crons ``scripts/sync.py``). This module adds the hands-off
option the Team tier asks for: a *persisted auto-sync policy* plus a background runner, so
the store — and every teammate's view of it — stays converged **on a cadence** (minimum
5 minutes) without anyone clicking.

Cadence-only on purpose. A fixed interval caps relay traffic to a known ceiling (at most a
handful of syncs an hour), whereas syncing on every edit would make load — and, on a
metered relay host, cost — scale with how much the team types. Each sync is a full-state
bundle per workspace (export + upload + pull peers), so predictable beats chatty.

House style (AGENTS.md §3): pure policy helpers + thin IO, exactly like
:mod:`engraphis.automation`. There is deliberately **no gate in here** — the sync-feature
gate lives at the entry points (the ``/api/sync/*`` routes; changing the policy is
admin-only in team mode, see ``inspector/auth.min_role``) and, non-bypassably, at the relay
itself, which verifies the license server-side. ``run_once`` re-uses the same audited
per-workspace sync the dashboard button already calls: it adds a *trigger*, never a new
trust boundary. A pulled bundle is still untrusted and still validated/clamped by
``SyncEngine.apply_bundle`` whether a human or the timer initiated the pull (SECURITY.md,
docs/SYNC.md).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

#: Off until the operator opts in; a 15-minute cadence balances freshness against relay
#: chatter, floored at ``MIN_CADENCE_MINUTES`` so a fat-fingered 0 can't hammer the relay.
DEFAULT_POLICY: dict = {
    "enabled": False,
    "cadence_minutes": 15,
}
MIN_CADENCE_MINUTES = 5

_POLICY_KEYS = set(DEFAULT_POLICY)


def policy_path() -> Path:
    """Where the auto-sync policy is persisted (next to the DB, else ~/.engraphis)."""
    override = os.environ.get("ENGRAPHIS_AUTOSYNC_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    db = os.environ.get("ENGRAPHIS_DB_PATH", "").strip()
    if db and db != ":memory:":
        try:
            return Path(db).expanduser().resolve().parent / "autosync.json"
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".engraphis" / "autosync.json"


def _read() -> dict:
    try:
        return json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def normalize_policy(raw: dict) -> dict:
    """Coerce arbitrary input into a safe, fully-populated policy (never raises).

    ``cadence_minutes`` is floored at :data:`MIN_CADENCE_MINUTES` (5) so neither a slip of
    the finger nor a crafted request can drive the relay faster than once every 5 minutes."""
    raw = raw if isinstance(raw, dict) else {}
    p = dict(DEFAULT_POLICY)
    for k in _POLICY_KEYS:
        if k in raw:
            p[k] = raw[k]
    p["enabled"] = bool(p["enabled"])
    try:
        p["cadence_minutes"] = max(MIN_CADENCE_MINUTES, int(p["cadence_minutes"] or 15))
    except (TypeError, ValueError):
        p["cadence_minutes"] = 15
    return p


def load_policy() -> dict:
    """The current policy plus last_run/last_result telemetry (safe on the free tier)."""
    raw = _read()
    p = normalize_policy(raw.get("policy", raw))
    p["last_run"] = raw.get("last_run")
    p["last_result"] = raw.get("last_result")
    return p


def _write(doc: dict) -> None:
    """Atomic temp-file + fsync + os.replace (mount-safe, per OPS_CONTRACT env note 7)."""
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_policy(policy: dict) -> dict:
    """Persist the auto-sync policy, preserving last_run/last_result telemetry."""
    existing = _read()
    _write({"policy": normalize_policy(policy),
            "last_run": existing.get("last_run"),
            "last_result": existing.get("last_result")})
    return load_policy()


def due(policy: dict, *, now: Optional[float] = None) -> bool:
    """True when an enabled policy is due to run (cadence elapsed since last_run)."""
    if not policy.get("enabled"):
        return False
    now = time.time() if now is None else now
    last = policy.get("last_run")
    if not last:
        return True
    try:
        return (now - float(last)) >= normalize_policy(policy)["cadence_minutes"] * 60.0
    except (TypeError, ValueError):
        return True


def _record(summary: dict, *, now: Optional[float] = None) -> None:
    """Stamp last_run + a compact last_result onto the persisted policy (best-effort)."""
    now = time.time() if now is None else now
    existing = _read()
    try:
        _write({"policy": normalize_policy(existing.get("policy", existing)),
                "last_run": float(now),
                "last_result": {
                    "at": float(now),
                    "workspaces": int(summary.get("workspaces", 0) or 0),
                    "exported": int(summary.get("exported", 0) or 0),
                    "added": int(summary.get("added", 0) or 0),
                    "updated": int(summary.get("updated", 0) or 0),
                    "errors": len(summary.get("errors", []) or []),
                }})
    except OSError:
        pass


def run_once(service: Any = None, *, now: Optional[float] = None,
             record: bool = True) -> dict:
    """Run one auto-sync pass across every workspace — if this device is licensed for sync
    and has a key configured. Never raises: a relay/transport failure lands in the
    summary's ``errors`` so the background loop keeps ticking. Records last_run/last_result
    unless ``record`` is False. Returns the sync summary, or a ``{"skipped": ...}`` note
    when the plan/key isn't ready (so the loop no-ops cheaply instead of hammering the
    relay)."""
    from engraphis import licensing
    if not licensing.has_feature("sync"):
        return {"skipped": "unlicensed"}
    if not licensing._read_key_material():
        return {"skipped": "no-key"}
    from engraphis.routes import v2_api
    svc = service if service is not None else v2_api.service()
    summary = v2_api._sync_all(svc)
    try:                    # keep the dashboard's "last synced" line honest for auto runs
        v2_api._SYNC_STATE["last"] = summary
    except Exception:  # noqa: BLE001
        pass
    if record:
        _record(summary, now=now)
    return summary
