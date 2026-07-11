"""Automated maintenance — the Pro "set it and forget it" layer.

Free users consolidate and prune by hand from the dashboard. Pro adds a *persisted
maintenance policy* plus a runner (dashboard button, HTTP endpoint, and the
``scripts/auto_maintain.py`` CLI that pm2/cron calls) so the store keeps itself
clean on a cadence: scheduled consolidation with configurable clustering and a
retention/archival threshold.

House style (AGENTS.md §3): pure policy helpers + thin IO. The Pro gate lives inside
:func:`save_policy` and :func:`run_maintenance` (defense in depth) — the same
``require_feature("automation")`` every other paid surface funnels through, so a
bypass means editing the compiled licensing module, not deleting a route decorator.
No new dependency: the sweep itself is the already-tested ``MemoryService.consolidate``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

#: Conservative defaults — disabled until the operator opts in, daily cadence, and the
#: same clustering/archival thresholds the manual Consolidate view uses.
DEFAULT_POLICY: dict = {
    "enabled": False,
    "cadence_hours": 24,
    "consolidate": True,
    "min_cluster": 3,
    "archive_below": 0.05,
    "workspaces": [],  # empty = every workspace the caller can see
}

_POLICY_KEYS = set(DEFAULT_POLICY)


def policy_path() -> Path:
    """Where the maintenance policy is persisted (next to the DB, else ~/.engraphis)."""
    override = os.environ.get("ENGRAPHIS_AUTOMATION_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    db = os.environ.get("ENGRAPHIS_DB_PATH", "").strip()
    if db and db != ":memory:":
        try:
            return Path(db).expanduser().resolve().parent / "automation.json"
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".engraphis" / "automation.json"


def _read() -> dict:
    try:
        return json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def normalize_policy(raw: dict) -> dict:
    """Coerce arbitrary input into a safe, fully-populated policy (never raises)."""
    raw = raw if isinstance(raw, dict) else {}
    p = dict(DEFAULT_POLICY)
    for k in _POLICY_KEYS:
        if k in raw:
            p[k] = raw[k]
    p["enabled"] = bool(p["enabled"])
    p["consolidate"] = bool(p["consolidate"])
    try:
        p["cadence_hours"] = max(1, int(p["cadence_hours"] or 24))
    except (TypeError, ValueError):
        p["cadence_hours"] = 24
    try:
        p["min_cluster"] = min(20, max(2, int(p["min_cluster"] or 3)))
    except (TypeError, ValueError):
        p["min_cluster"] = 3
    try:
        p["archive_below"] = min(0.5, max(0.0, float(p["archive_below"])))
    except (TypeError, ValueError):
        p["archive_below"] = 0.05
    wss = p.get("workspaces") or []
    p["workspaces"] = [str(w) for w in wss] if isinstance(wss, list) else []
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
    """Persist a maintenance policy. Pro-gated (``automation``)."""
    from engraphis.licensing import require_feature
    require_feature("automation")
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
        return (now - float(last)) >= policy.get("cadence_hours", 24) * 3600.0
    except (TypeError, ValueError):
        return True


def run_maintenance(service: Any, *, dry_run: bool = True,
                    policy: Optional[dict] = None, record: bool = True,
                    now: Optional[float] = None) -> dict:
    """Apply the maintenance policy across its target workspaces. Pro-gated.

    Runs the same audited ``MemoryService.consolidate`` the dashboard's Consolidate
    view exposes, with the policy's ``min_cluster``/``archive_below``. ``dry_run``
    previews without mutating. One failing workspace is captured per-entry and never
    aborts the sweep. Unless ``dry_run``, records ``last_run``/``last_result``."""
    from engraphis.licensing import require_feature
    require_feature("automation")
    now = time.time() if now is None else now
    pol = normalize_policy(policy) if policy is not None else load_policy()
    targets = pol["workspaces"]
    if not targets:
        wss = (service.list_workspaces() or {}).get("workspaces") or []
        targets = [w["name"] for w in wss]
    runs = []
    for ws in targets:
        entry: dict = {"workspace": ws}
        try:
            if pol["consolidate"]:
                entry["consolidate"] = service.consolidate(
                    workspace=ws, dry_run=dry_run,
                    min_cluster=pol["min_cluster"],
                    archive_below=pol["archive_below"])
        except Exception as exc:  # noqa: BLE001 - isolate a bad workspace
            entry["error"] = str(exc)
        runs.append(entry)
    result = {"ran_at": int(now), "dry_run": dry_run,
              "workspaces": [r["workspace"] for r in runs], "runs": runs}
    if record and not dry_run:
        existing = _read()
        try:
            _write({"policy": normalize_policy(existing.get("policy", existing) or pol),
                    "last_run": int(now),
                    "last_result": {"ran_at": int(now),
                                    "workspaces": result["workspaces"],
                                    "dry_run": False}})
        except OSError:
            pass
    return result
