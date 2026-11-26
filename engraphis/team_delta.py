"""Client transport for Engraphis Team delta pull and proposal push.

Connects a local SQLite Engraphis database to hosted Team endpoints:
- ``POST /v1/team/workspaces/{workspace}/delta`` (keyset-paginated approved memories + tombstones)
- ``POST /v1/team/workspaces/{workspace}/propose`` (batched local candidates for Team review)

Security invariants:
- Base URLs are validated with ``validate_cloud_base_url`` (HTTPS or loopback HTTP only).
- HTTP opener disables proxies (``ProxyHandler({})``) and refuses all 3xx redirects so
  bearer credentials never leave the bound origin.
- Pulled items undergo client-side ``reject_secrets`` and ``assess_untrusted_payload``
  defense-in-depth before local caching.
- ``unapprove`` and ``erase`` tombstones purge local rows and vector entries via
  ``secure_erase_memory(_defer_maintenance=True)`` (never exporting peer tombstones).
- ``push_team_proposals`` fails closed on ``personal`` or unreadable workspace visibility,
  excludes ``session``/``user``/quarantined/already-synced memories, and runs per-item
  ``reject_secrets`` before sending a single byte over HTTP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

from engraphis.config import RETIRED_RELAY_URLS
from engraphis.core.poisoning import assess_untrusted_payload, inspection_eligible, prompt_eligible
from engraphis.core.secrets import SecretDetectedError, reject_secrets
from engraphis.hosted_client import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    build_pinned_https_opener,
    validate_cloud_base_url,
)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PROPOSE_BATCH_ITEMS = 50
MAX_PROPOSE_BATCH_BYTES = 900_000
MAX_TRACKED_PROPOSALS = 5_000
MAX_CURSOR_CHARS = 512
_ALLOWED_MTYPES = frozenset({"semantic", "episodic", "working", "procedural"})
_ALLOWED_PUSH_SCOPES = frozenset({"workspace", "repo", "project", "global"})
_ALLOWED_PUSH_SENSITIVITIES = frozenset({"normal", "sensitive"})
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RETIRED_ORIGINS = frozenset(
    urlsplit(u.strip()).scheme.lower() + "://" + (urlsplit(u.strip()).hostname or "").lower()
    + f":{urlsplit(u.strip()).port or (443 if urlsplit(u.strip()).scheme.lower() == 'https' else 80)}"
    for u in RETIRED_RELAY_URLS
)

TeamTransportFn = Callable[[str, Mapping[str, Any], Mapping[str, str], float], dict[str, Any]]


class TeamDeltaError(RuntimeError):
    """Raised when a Team delta sync or proposal operation fails."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on 3xx redirects so bearer headers are never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise TeamDeltaError(f"team API redirect refused (HTTP {code})")


def canonical_origin(url: str) -> str:
    """Return canonical ``scheme://host:effective_port`` for strict origin binding."""
    cleaned = validate_cloud_base_url(url)
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{bracketed}:{port}"


def _reject_retired_origin(url: str) -> str:
    origin = canonical_origin(url)
    if origin in _RETIRED_ORIGINS:
        raise TeamDeltaError("configured Team API URL points to a retired host")
    return origin


def _validated_bearer(bearer: str) -> str:
    """Validate single-line printable ASCII bearer token without echoing its value."""
    auth_value = str(bearer or "").strip()
    if (
        not auth_value
        or len(auth_value) > 4096
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in auth_value)
    ):
        raise TeamDeltaError("invalid or missing Team bearer credential")
    return auth_value


def validate_team_origin_binding(team_url: str, configured_origin: Optional[str]) -> str:
    """Verify the target URL is valid, non-retired, and matches the configured credential origin."""
    base_url = validate_cloud_base_url(team_url)
    target = _reject_retired_origin(base_url)
    if not configured_origin or not str(configured_origin).strip():
        raise TeamDeltaError(
            "ENGRAPHIS_TEAM_TOKEN requires ENGRAPHIS_TEAM_TOKEN_ORIGIN bound to the Team API origin"
        )
    try:
        bound = _reject_retired_origin(str(configured_origin))
    except (ValueError, TeamDeltaError) as exc:
        raise TeamDeltaError(f"invalid ENGRAPHIS_TEAM_TOKEN_ORIGIN: {exc}") from None
    if bound != target:
        raise TeamDeltaError(
            "configured Team credential is not bound to the target Team API origin"
        )
    return base_url


def _scope_hash(workspace: str, repo: Optional[str], origin: str) -> str:
    raw = f"{workspace}\0{repo or ''}\0{origin}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def cursor_state_key(workspace: str, repo: Optional[str], origin: str) -> str:
    return f"team_delta_cursor:{_scope_hash(workspace, repo, origin)}"


def proposed_state_key(workspace: str, repo: Optional[str], origin: str) -> str:
    return f"team_delta_proposed:{_scope_hash(workspace, repo, origin)}"


def _content_hash(content: str, title: str = "", mtype: str = "semantic", repo: Optional[str] = None) -> str:
    payload = json.dumps(
        {"content": content, "title": title, "mtype": mtype, "repo": repo or ""},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json_state(store: Any, key: str) -> dict[str, Any]:
    raw = store.get_sync_state(key)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_json_state(store: Any, key: str, value: Mapping[str, Any]) -> None:
    store.set_sync_state(key, json.dumps(dict(value), sort_keys=True, separators=(",", ":")))


def _default_http_post(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(dict(payload), separators=(",", ":"), allow_nan=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "engraphis-team-delta/1.8",
            **dict(headers),
        },
    )
    opener = build_pinned_https_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TeamDeltaError("team API response exceeded 10 MiB safety limit")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise TeamDeltaError("team API returned a non-object JSON payload")
            return parsed
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err_body = exc.read(1024).decode("utf-8", errors="replace").strip()
            if err_body:
                parsed_err = json.loads(err_body)
                if isinstance(parsed_err, dict) and parsed_err.get("detail"):
                    detail = f": {str(parsed_err['detail'])[:200]}"
        except Exception:
            pass
        raise TeamDeltaError(f"team API rejected request (HTTP {exc.code}){detail}") from None
    except urllib.error.URLError as exc:
        raise TeamDeltaError(f"team API request failed: {exc.reason}") from None
    except (OSError, ValueError) as exc:
        # Never echo raw ValueError text from http.client (may contain Authorization header).
        raise TeamDeltaError(f"team API transport error ({type(exc).__name__})") from None


def _validate_workspace_name(workspace: str) -> str:
    cleaned = str(workspace or "").strip()
    if not _WORKSPACE_NAME_RE.fullmatch(cleaned):
        raise TeamDeltaError("invalid workspace name for Team delta sync")
    return cleaned


def _existing_team_memories(
    service: Any,
    workspace_id: str,
    *,
    include_retired: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Index local memories carrying ``metadata.team_memory_id``."""
    from engraphis.core.store import _loads

    sql = (
        "SELECT m.id, m.metadata, m.valid_to, m.expired_at, r.name AS repo_name "
        "FROM memories m "
        "LEFT JOIN repos r ON r.id = m.repo_id "
        "WHERE m.workspace_id = ?"
    )
    if not include_retired:
        sql += " AND m.valid_to IS NULL AND m.expired_at IS NULL"
    rows = service.store.conn.execute(sql, (workspace_id,)).fetchall()
    by_team_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        meta = _loads(row["metadata"], {})
        if not isinstance(meta, dict):
            continue
        team_id = meta.get("team_memory_id")
        if isinstance(team_id, str) and team_id.strip():
            by_team_id.setdefault(team_id.strip(), []).append(
                {
                    "local_id": str(row["id"]),
                    "repo": row["repo_name"],
                    "content_hash": str(meta.get("team_content_hash") or ""),
                    "active": row["valid_to"] is None and row["expired_at"] is None,
                }
            )
    return by_team_id


def _purge_local_cache_entry(service: Any, local_id: str) -> bool:
    """Crash-safely hard-purge a revoked/erased Team cache entry without peer tombstones."""
    from contextlib import nullcontext
    from engraphis.core.vector_repair import (
        index_repair_identity,
        vector_index_shares_store_transaction,
    )

    index = getattr(service.engine, "index", None)
    write_guard = getattr(service.engine, "_write_guard", None)
    guard_ctx: Any = write_guard() if callable(write_guard) else nullcontext()
    shared_tx = index is not None and vector_index_shares_store_transaction(index, service.store)
    repair_target = index_repair_identity(index, service.store) if index is not None else None
    attempted_ids: set[str] = set()
    confirmed_ids: set[str] = set()

    def _delete_vectors(ids: list[str]) -> None:
        if not ids or index is None:
            return
        delete_fn = getattr(index, "delete", None)
        if callable(delete_fn):
            if shared_tx:
                delete_fn(ids, commit=False)
            else:
                delete_fn(ids)
            return
        remove_fn = getattr(index, "remove", None)
        if callable(remove_fn):
            for tid in ids:
                remove_fn(tid)

    with guard_ctx:
        try:
            with service.store.write_transaction():
                target_ids: list[str] = [local_id]
                target_fn = getattr(service.store, "secure_erase_target_ids", None)
                if callable(target_fn):
                    raw_targets = target_fn(local_id)
                    if isinstance(raw_targets, (list, tuple, set)):
                        resolved = [str(tid) for tid in raw_targets if tid]
                        if resolved:
                            target_ids = resolved
                if repair_target is not None:
                    service.store.queue_vector_index_repairs(repair_target, target_ids)
                attempted_ids.update(target_ids)
                _delete_vectors(target_ids)
                confirmed_ids.update(target_ids)
                try:
                    service.store.secure_erase_memory(
                        local_id, actor="team-delta-sync", _defer_maintenance=True
                    )
                except KeyError:
                    return False
                if repair_target is not None and confirmed_ids:
                    service.store.acknowledge_vector_index_repairs(
                        repair_target,
                        service.store.vector_index_repair_generations(repair_target, confirmed_ids),
                    )
        except BaseException as erase_exc:
            if repair_target is not None and attempted_ids:
                try:
                    service.store.queue_vector_index_repairs(repair_target, attempted_ids)
                except BaseException as repair_exc:
                    raise repair_exc from erase_exc
            raise
    return True


def _resolve_sync_target(workspace: str, team_url: str, bearer: str) -> tuple[str, str, str, str]:
    ws_name = _validate_workspace_name(workspace)
    base_url = validate_cloud_base_url(team_url)
    origin = _reject_retired_origin(base_url)
    auth_value = _validated_bearer(bearer)
    return ws_name, base_url, origin, auth_value


def pull_team_delta(
    service: Any,
    *,
    workspace: str,
    team_url: str,
    bearer: str,
    repo: Optional[str] = None,
    limit: int = 100,
    max_pages: int = 20,
    dry_run: bool = False,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    transport: Optional[TeamTransportFn] = None,
) -> dict[str, Any]:
    """Pull approved Team memories incrementally and apply permanent Team tombstones locally."""
    ws_name, base_url, origin, auth_value = _resolve_sync_target(workspace, team_url, bearer)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 500:
        raise TeamDeltaError("delta limit must be between 1 and 500")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1 or max_pages > 200:
        raise TeamDeltaError("max_pages must be between 1 and 200")

    ws_id = service._lookup_workspace(ws_name)
    if ws_id is not None:
        _check_workspace_shareable(service, ws_name)
    elif not dry_run:
        ws_id = service.store.get_or_create_workspace(ws_name)

    state_key = cursor_state_key(ws_name, repo, origin)
    saved = _load_json_state(service.store, state_key)
    cursor = str(saved.get("cursor") or "")
    raw_seq = saved.get("tombstone_since", 0)
    tombstone_since = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq >= 0 else 0

    post_fn = transport or _default_http_post
    endpoint = f"{base_url}/v1/team/workspaces/{quote(ws_name, safe='')}/delta"
    headers = {"Authorization": f"Bearer {auth_value}"}

    existing_all = (
        _existing_team_memories(service, ws_id, include_retired=True)
        if ws_id is not None
        else {}
    )
    existing: dict[str, list[dict[str, Any]]] = {
        tid: [entry for entry in entries if entry.get("active")]
        for tid, entries in existing_all.items()
        if any(entry.get("active") for entry in entries)
    }
    added = 0
    updated = 0
    unchanged = 0
    blocked_items = 0
    tombstones_retired = 0
    tombstones_purged = 0
    tombstone_errors = 0
    pages_fetched = 0
    has_more = False
    erased_any = False

    from engraphis.service import ValidationError

    for _ in range(max_pages):
        req_payload: dict[str, Any] = {
            "cursor": cursor,
            "tombstone_since": tombstone_since,
            "limit": limit,
        }
        if repo:
            req_payload["repo"] = repo
        resp = post_fn(endpoint, req_payload, headers, timeout)
        pages_fetched += 1

        items = resp.get("items")
        tombstones = resp.get("tombstones")
        if not isinstance(items, list) or not isinstance(tombstones, list):
            raise TeamDeltaError("team delta response missing items/tombstones arrays")

        raw_next_cursor = resp.get("next_cursor")
        if raw_next_cursor is None:
            next_cursor = cursor
        elif isinstance(raw_next_cursor, str) and len(raw_next_cursor) <= MAX_CURSOR_CHARS:
            next_cursor = raw_next_cursor
        else:
            raise TeamDeltaError("team delta response returned invalid cursor")
        next_seq = resp.get("next_tombstone_sequence", tombstone_since)
        if not isinstance(next_seq, int) or isinstance(next_seq, bool) or next_seq < tombstone_since:
            raise TeamDeltaError("team delta response returned invalid tombstone sequence")
        has_more = bool(resp.get("has_more", False))
        if has_more and next_cursor == cursor and next_seq == tombstone_since:
            raise TeamDeltaError("team delta pagination made no progress")

        # 1. Apply approved items first (so a same-page tombstone wins deterministically).
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            # Fail closed unless the server explicitly attested reviewer approval and eligibility.
            if raw_item.get("review_state") != "approved" or raw_item.get("prompt_eligible") is not True:
                blocked_items += 1
                continue
            team_id = str(raw_item.get("id") or "").strip()
            content = _CONTROL_RE.sub("", str(raw_item.get("content") or "")).strip()
            title = _CONTROL_RE.sub("", str(raw_item.get("title") or "")).strip()
            mtype = str(raw_item.get("mtype") or "semantic").strip()
            if mtype not in _ALLOWED_MTYPES:
                mtype = "semantic"
            raw_item_repo = _CONTROL_RE.sub("", str(raw_item.get("repo") or "")).strip() or None
            if repo and raw_item_repo and raw_item_repo != repo:
                blocked_items += 1
                continue
            item_repo = raw_item_repo or repo
            if not team_id or not content:
                blocked_items += 1
                continue

            # Defense-in-depth client-side secret and poisoning screening.
            check_fields = [("content", content)]
            if title:
                check_fields.append(("title", title))
            if item_repo:
                check_fields.append(("repo", item_repo))
            try:
                reject_secrets(check_fields)
            except SecretDetectedError:
                blocked_items += 1
                continue
            poison = assess_untrusted_payload(content, title=title)
            if poison.quarantined:
                blocked_items += 1
                continue

            digest = _content_hash(content, title=title, mtype=mtype, repo=item_repo)
            prior_entries = existing.get(team_id, [])
            if prior_entries and any(e["content_hash"] == digest for e in prior_entries):
                unchanged += 1
                continue

            if dry_run:
                if prior_entries:
                    updated += 1
                else:
                    added += 1
                dry_entry = {"local_id": f"dry_{team_id}", "repo": item_repo, "content_hash": digest, "active": True}
                existing[team_id] = [dry_entry]
                existing_all.setdefault(team_id, []).append(dry_entry)
                continue

            # Write the validated revision FIRST, then retire superseded prior local copies.
            is_update = bool(prior_entries)
            try:
                stored = service.remember(
                    content,
                    title=title,
                    mtype=mtype,
                    workspace=ws_name,
                    repo=item_repo,
                    source="agent",
                    resolve_conflicts=False,
                    metadata={
                        "capture_mode": "team_delta_sync",
                        "team_memory_id": team_id,
                        "team_origin": origin,
                        "team_source": raw_item.get("source"),
                        "team_review_state": raw_item.get("review_state"),
                        "team_ingested_at": raw_item.get("ingested_at"),
                        "team_content_hash": digest,
                    },
                )
            except (SecretDetectedError, ValidationError):
                blocked_items += 1
                continue

            unretired: list[dict[str, Any]] = []
            for prior in prior_entries:
                try:
                    service.retire(
                        prior["local_id"],
                        workspace=ws_name,
                        repo=prior["repo"],
                        actor="team-delta-sync",
                        reason="Superseded by updated Team memory revision",
                    )
                    prior["active"] = False
                except Exception:
                    unretired.append(prior)

            new_local_id = str(stored.get("id") or "")
            new_entry = {
                "local_id": new_local_id,
                "repo": item_repo,
                "content_hash": digest,
                "active": True,
            }
            existing[team_id] = [*unretired, new_entry]
            existing_all.setdefault(team_id, []).append(new_entry)
            if is_update:
                updated += 1
            else:
                added += 1

        # 2. Apply permanent tombstones in monotonic sequence order; hold watermark and stop on error.
        def _tomb_seq(raw: Any) -> int:
            if not isinstance(raw, dict):
                return 0
            val = raw.get("sequence")
            return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0

        tombstone_failed = False
        applied_seq = tombstone_since
        ordered_tombs = sorted(
            enumerate(tombstones),
            key=lambda pair: (_tomb_seq(pair[1]), pair[0]),
        )
        for _, raw_tomb in ordered_tombs:
            if not isinstance(raw_tomb, dict):
                continue
            target_team_id = str(raw_tomb.get("memory_id") or "").strip()
            kind = str(raw_tomb.get("kind") or "").strip()
            tomb_seq = _tomb_seq(raw_tomb)
            if not target_team_id or kind not in {"retire", "unapprove", "erase"}:
                if not tombstone_failed and tomb_seq > applied_seq:
                    applied_seq = tomb_seq
                continue
            matches = (
                existing.get(target_team_id, [])
                if kind == "retire"
                else existing_all.get(target_team_id, [])
            )
            if not matches:
                if not tombstone_failed and tomb_seq > applied_seq:
                    applied_seq = tomb_seq
                continue
            if dry_run:
                if kind == "retire":
                    tombstones_retired += len(matches)
                    existing.pop(target_team_id, None)
                else:
                    tombstones_purged += len(matches)
                    existing.pop(target_team_id, None)
                    existing_all.pop(target_team_id, None)
                if tomb_seq > applied_seq:
                    applied_seq = tomb_seq
                continue

            if kind == "retire":
                entry_failed = False
                remaining_entries: list[dict[str, Any]] = []
                for entry in matches:
                    try:
                        service.retire(
                            entry["local_id"],
                            workspace=ws_name,
                            repo=entry["repo"],
                            actor="team-delta-sync",
                            reason="Team memory retired",
                        )
                        entry["active"] = False
                        tombstones_retired += 1
                    except Exception:
                        tombstone_errors += 1
                        entry_failed = True
                        remaining_entries.append(entry)
                if entry_failed:
                    existing[target_team_id] = remaining_entries
                    tombstone_failed = True
                else:
                    existing.pop(target_team_id, None)
                    if not tombstone_failed and tomb_seq > applied_seq:
                        applied_seq = tomb_seq
            else:
                entry_failed = False
                remaining_all: list[dict[str, Any]] = []
                for entry in matches:
                    try:
                        purged = _purge_local_cache_entry(service, entry["local_id"])
                    except Exception:
                        tombstone_errors += 1
                        entry_failed = True
                        remaining_all.append(entry)
                        continue
                    if purged:
                        tombstones_purged += 1
                        erased_any = True
                    elif service.store.get_memory(entry["local_id"]) is not None:
                        tombstone_errors += 1
                        entry_failed = True
                        remaining_all.append(entry)
                if entry_failed:
                    existing_all[target_team_id] = remaining_all
                    existing[target_team_id] = [e for e in remaining_all if e.get("active")]
                    tombstone_failed = True
                else:
                    existing.pop(target_team_id, None)
                    existing_all.pop(target_team_id, None)
                    if not tombstone_failed and tomb_seq > applied_seq:
                        applied_seq = tomb_seq

        cursor = next_cursor
        tombstone_since = next_seq if not tombstone_failed else applied_seq
        if tombstone_failed:
            has_more = True

        if not dry_run:
            _save_json_state(
                service.store,
                state_key,
                {
                    "origin": origin,
                    "workspace": ws_name,
                    "repo": repo,
                    "cursor": cursor,
                    "tombstone_since": tombstone_since,
                    "last_sync_at": time.time(),
                },
            )

        if tombstone_failed or not has_more:
            break

    if erased_any and not dry_run:
        run_maint = getattr(service.store, "run_secure_erase_maintenance", None)
        if callable(run_maint):
            try:
                run_maint()
            except Exception:
                pass

    return {
        "mode": "team_delta_pull",
        "workspace": ws_name,
        "repo": repo,
        "origin": origin,
        "dry_run": dry_run,
        "pages_fetched": pages_fetched,
        "has_more": has_more,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "blocked_items": blocked_items,
        "tombstones_retired": tombstones_retired,
        "tombstones_purged": tombstones_purged,
        "tombstone_errors": tombstone_errors,
        "cursor": cursor,
        "tombstone_since": tombstone_since,
    }


def _check_workspace_shareable(service: Any, workspace_name: str) -> str:
    """Fail closed if the local workspace is personal, missing, or has unreadable settings."""
    row = service.store.conn.execute(
        "SELECT id, settings FROM workspaces WHERE name = ?",
        (workspace_name,),
    ).fetchone()
    if row is None:
        raise TeamDeltaError(f"no local workspace named '{workspace_name}'")
    try:
        ws_settings = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError, RecursionError):
        ws_settings = None
    if not isinstance(ws_settings, dict):
        raise TeamDeltaError("workspace settings are unreadable; refusing to sync with Team")
    visibility = ws_settings.get("visibility")
    if visibility == "personal":
        raise TeamDeltaError("personal workspaces are device-local and cannot sync with Team")
    if visibility not in (None, "shared"):
        raise TeamDeltaError("workspace visibility is invalid; refusing to sync with Team")
    return str(row["id"])


def push_team_proposals(
    service: Any,
    *,
    workspace: str,
    team_url: str,
    bearer: str,
    repo: Optional[str] = None,
    source_filter: str = "passive-hook",
    max_batch: int = MAX_PROPOSE_BATCH_ITEMS,
    dry_run: bool = False,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    transport: Optional[TeamTransportFn] = None,
) -> dict[str, Any]:
    """Push eligible local candidate memories into the hosted Team review queue."""
    from engraphis.core.store import _loads

    ws_name, base_url, origin, auth_value = _resolve_sync_target(workspace, team_url, bearer)
    if isinstance(max_batch, bool) or not isinstance(max_batch, int) or max_batch < 1 or max_batch > MAX_PROPOSE_BATCH_ITEMS:
        raise TeamDeltaError(f"max_batch must be between 1 and {MAX_PROPOSE_BATCH_ITEMS}")

    ws_id = _check_workspace_shareable(service, ws_name)

    sql = [
        "SELECT m.id, m.content, m.title, m.mtype, m.scope, m.sensitivity, "
        "m.provenance, m.metadata, r.name AS repo_name "
        "FROM memories m "
        "LEFT JOIN repos r ON r.id = m.repo_id "
        "WHERE m.workspace_id = ? "
        "AND m.valid_to IS NULL AND m.expired_at IS NULL "
        "AND LOWER(COALESCE(m.sensitivity, '')) IN ('normal', 'sensitive')"
    ]
    params: list[object] = [ws_id]
    if repo:
        sql.append("AND r.name = ?")
        params.append(repo)
    sql.append("ORDER BY COALESCE(m.ingested_at, 0.0) ASC, m.id ASC")
    rows = service.store.conn.execute(" ".join(sql), params).fetchall()

    prop_key = proposed_state_key(ws_name, repo, origin)
    proposed_map = _load_json_state(service.store, prop_key)
    tracked: dict[str, str] = {
        str(k): str(v) for k, v in proposed_map.items() if isinstance(k, str) and isinstance(v, str)
    }

    candidates: list[tuple[str, str, dict[str, Any], int]] = []
    skipped_already_proposed = 0
    blocked_count = 0
    skipped_oversized = 0
    skipped_scope_or_quarantine = 0

    for row in rows:
        local_id = str(row["id"])
        scope_val = str(row["scope"] or "").strip().lower()
        sens_val = str(row["sensitivity"] or "").strip().lower()
        if scope_val not in _ALLOWED_PUSH_SCOPES or sens_val not in _ALLOWED_PUSH_SENSITIVITIES:
            skipped_scope_or_quarantine += 1
            continue

        prov = _loads(row["provenance"], {})
        meta = _loads(row["metadata"], {})
        if not isinstance(prov, dict):
            prov = {}
        if not isinstance(meta, dict):
            meta = {}

        if meta.get("team_memory_id") or meta.get("capture_mode") == "team_delta_sync":
            continue
        if not inspection_eligible(prov, meta):
            skipped_scope_or_quarantine += 1
            continue

        row_source = str(prov.get("source") or "").strip().lower()
        if source_filter == "all":
            if not prompt_eligible(prov, meta):
                skipped_scope_or_quarantine += 1
                continue
        elif row_source != source_filter.strip().lower():
            continue

        content = _CONTROL_RE.sub("", str(row["content"] or "")).strip()
        title = _CONTROL_RE.sub("", str(row["title"] or "")).strip()
        mtype = str(row["mtype"] or "semantic").strip()
        if mtype not in _ALLOWED_MTYPES:
            mtype = "semantic"
        item_repo = _CONTROL_RE.sub("", str(row["repo_name"] or "")).strip() or None
        if not content:
            continue

        digest = _content_hash(content, title=title, mtype=mtype, repo=item_repo)
        if tracked.get(local_id) == digest:
            skipped_already_proposed += 1
            continue

        fields = [("content", content)]
        if title:
            fields.append(("title", title))
        if item_repo:
            fields.append(("repo", item_repo))
        try:
            reject_secrets(fields)
        except SecretDetectedError:
            blocked_count += 1
            continue
        if assess_untrusted_payload(content, title=title).quarantined:
            skipped_scope_or_quarantine += 1
            continue

        item_payload: dict[str, Any] = {
            "content": content,
            "title": title[:200],
            "mtype": mtype,
        }
        if item_repo:
            item_payload["repo"] = item_repo[:128]
        encoded_size = len(json.dumps(item_payload, separators=(",", ":")).encode("utf-8"))
        if encoded_size > MAX_PROPOSE_BATCH_BYTES or len(content) > 60_000:
            skipped_oversized += 1
            continue
        candidates.append((local_id, digest, item_payload, encoded_size))

    batches: list[list[tuple[str, str, dict[str, Any], int]]] = []
    current_batch: list[tuple[str, str, dict[str, Any], int]] = []
    current_bytes = 2
    for cand in candidates:
        cand_bytes = cand[3] + 1
        if current_batch and (
            len(current_batch) >= max_batch or current_bytes + cand_bytes > MAX_PROPOSE_BATCH_BYTES
        ):
            batches.append(current_batch)
            current_batch = []
            current_bytes = 2
        current_batch.append(cand)
        current_bytes += cand_bytes
    if current_batch:
        batches.append(current_batch)

    post_fn = transport or _default_http_post
    endpoint = f"{base_url}/v1/team/workspaces/{quote(ws_name, safe='')}/propose"
    headers = {"Authorization": f"Bearer {auth_value}"}

    proposed_count = 0
    failed_proposals = 0
    batches_sent = 0
    if not dry_run:
        for batch in batches:
            body = {
                "confirmed_shared_hosting": True,
                "memories": [item for _, _, item, _ in batch],
            }
            resp = post_fn(endpoint, body, headers, timeout)
            batches_sent += 1
            results = resp.get("results") if isinstance(resp, dict) else None
            if isinstance(results, list) and len(results) == len(batch):
                for (local_id, digest, _, _), item_res in zip(batch, results):
                    is_failed = (
                        not isinstance(item_res, dict)
                        or bool(item_res.get("error"))
                        or item_res.get("ok") is False
                        or str(item_res.get("status") or "").lower()
                        in {"error", "failed", "rejected"}
                    )
                    if is_failed:
                        failed_proposals += 1
                    else:
                        tracked[local_id] = digest
                        proposed_count += 1
            else:
                failed_proposals += len(batch)
            if len(tracked) > MAX_TRACKED_PROPOSALS:
                excess = len(tracked) - MAX_TRACKED_PROPOSALS
                for old_key in list(tracked.keys())[:excess]:
                    tracked.pop(old_key, None)
            _save_json_state(service.store, prop_key, tracked)
    else:
        proposed_count = len(candidates)

    return {
        "mode": "team_delta_propose",
        "workspace": ws_name,
        "repo": repo,
        "origin": origin,
        "source_filter": source_filter,
        "dry_run": dry_run,
        "proposed_count": proposed_count,
        "failed_proposals": failed_proposals,
        "batches_sent": batches_sent,
        "skipped_already_proposed": skipped_already_proposed,
        "blocked_count": blocked_count,
        "skipped_oversized": skipped_oversized,
        "skipped_scope_or_quarantine": skipped_scope_or_quarantine,
    }


def read_team_delta_status_entries(conn: Any, workspace: str, repo: Optional[str] = None) -> list[dict[str, Any]]:
    """Read-only status lookup of saved Team delta watermarks from ``sync_state``."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE 'team_delta_cursor:%' ORDER BY key ASC"
        ).fetchall()
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for row in rows:
        raw = row[1]
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(parsed, dict) or parsed.get("workspace") != workspace:
            continue
        if repo is not None and parsed.get("repo") != repo:
            continue
        raw_tseq = parsed.get("tombstone_since")
        tseq = raw_tseq if isinstance(raw_tseq, int) and not isinstance(raw_tseq, bool) and raw_tseq >= 0 else 0
        entries.append(
            {
                "origin": str(parsed.get("origin") or ""),
                "repo": parsed.get("repo"),
                "cursor": str(parsed.get("cursor") or ""),
                "tombstone_since": tseq,
            }
        )
    return entries


def resolve_team_url(explicit_url: Optional[str]) -> str:
    """Resolve explicit ``--team <url>`` or ``ENGRAPHIS_TEAM_URL`` (rejects retired hosts)."""
    candidate = str(explicit_url or "").strip() or os.environ.get("ENGRAPHIS_TEAM_URL", "").strip()
    if not candidate:
        raise TeamDeltaError(
            "--team needs a Team API URL: pass --team <url> or set ENGRAPHIS_TEAM_URL"
        )
    cleaned = validate_cloud_base_url(candidate)
    _reject_retired_origin(cleaned)
    return cleaned
