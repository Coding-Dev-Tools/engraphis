"""v1-dashboard API adapter over the v2 MemoryService (engraphis/service.py).

Serves the restored v1 dashboard on the *v2* engine — same look, real (v2) data.
Every route is under /api and returns plain JSON the dashboard's JS consumes. The open
runtime is single-user and local; hosted Team and managed feature authority live in
Engraphis Cloud.
"""
from __future__ import annotations

import datetime as _datetime
import json
import hmac
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
import weakref
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.config import DEFAULT_RELAY_URL, canonicalize_relay_url, settings
from engraphis.core.poisoning import prompt_eligible
from engraphis.core.scoring import normalize
from engraphis.service import (
    GraphIndexRebuilding,
    GraphSceneCapacityExceeded,
    MemoryService,
    ValidationError,
)
from engraphis.core.store import _escape_like
from engraphis.core.textutil import jaccard, tokenize

router = APIRouter(prefix="/api", tags=["dashboard"])
logger = logging.getLogger("engraphis.api")

_service: Optional[MemoryService] = None
_AUTOMATION_BOOTSTRAP_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_AUTOMATION_BOOTSTRAP_LOCKS_GUARD = threading.Lock()
_KEYWORD_SCORE_SEMANTICS = {
    "relative_score": (
        "Query-relative lexical Jaccard score, min-max normalized among the returned "
        "keyword-fallback memories. It is not a confidence value or threshold."
    ),
    "absolute_support": (
        "Absolute lexical Jaccard support in [0, 1]. Semantic support is unavailable "
        "while the dashboard is using keyword fallback."
    ),
}


def _automation_bootstrap_lock(organization_id: str, workspace_id: str) -> threading.Lock:
    """Return the process-local serialization lock for one hosted workspace bootstrap.

    The first Automation view sends a bounded but sensitive snapshot to the private service.
    Dashboard tabs often issue concurrent GETs, so durable phase tracking alone cannot prevent
    two requests from both observing an empty phase before either has recorded its upload.
    The local dashboard runs one process; this keyed lock covers that race without broadening a
    workspace lock or changing the private service protocol.
    """

    key = (str(organization_id), str(workspace_id))
    with _AUTOMATION_BOOTSTRAP_LOCKS_GUARD:
        return _AUTOMATION_BOOTSTRAP_LOCKS.setdefault(key, threading.Lock())


def _invalid_request() -> HTTPException:
    """Return the stable public boundary for service-layer validation failures.

    ``ValidationError`` can be raised after processing client-controlled names, ids, and
    paths.  Its message is useful for local diagnostics, but is not a safe API contract:
    it could echo an untrusted value or a future implementation detail.  Keep the HTTP
    response deliberately fixed and never log the error text at API call sites.
    """
    return HTTPException(status_code=400, detail={"error": "invalid request"})


class _HttpCodeIndexConfigurationError(RuntimeError):
    """Raised when the operator's HTTP indexing boundary is unsafe."""


def _http_index_configuration_error() -> HTTPException:
    """Keep operator configuration failures distinct from invalid client paths."""
    return HTTPException(status_code=500, detail={"error": "internal server error"})


def _sanitized_http_exception(status_code: object) -> HTTPException:
    """Keep a downstream HTTP status without propagating its detail or traceback.

    A service dependency can deliberately signal an HTTP status, but its exception
    object and detail are not part of this route's public contract.  Preserve a valid
    error-class status while returning a route-owned, static body.
    """
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        if status_code < 500:
            return HTTPException(status_code=status_code, detail={"error": "request rejected"})
        return HTTPException(status_code=status_code, detail={"error": "internal server error"})
    return HTTPException(status_code=500, detail={"error": "internal server error"})


def service() -> MemoryService:
    """Lazily bind a single MemoryService to the configured store (the live v2 DB)."""
    global _service
    if _service is None:
        _service = MemoryService.create(
            settings.db_path, embed_model=settings.embed_model,
            embed_dim=settings.embed_dim or 384)
    return _service


def set_service(svc: MemoryService) -> None:
    """Inject a service (tests / the dashboard app).

    Close the previously-bound service's store connection first so its SQLite/WAL
    handle can't leak across injections and hold a lock on the DB file — under heavy
    test churn a deferred GC close collided with the next MemoryService.create on the
    same path and surfaced as an intermittent ``database is locked``."""
    global _service
    prev = _service
    if prev is not None:
        try:
            prev.store.close()
        except Exception:  # noqa: BLE001 — never block the swap on a close error
            pass
    _service = svc


def _run(fn, *a, **k):
    """Call a service method, mapping validation errors to 400 and the rest to 500."""
    try:
        return fn(*a, **k)
    except GraphIndexRebuilding as exc:
        logger.info("graph index unavailable (%s, job_id=%s)", type(exc).__name__, exc.job_id)
        raise HTTPException(status_code=409, detail={
            "error": f"graph index rebuilding (job {exc.job_id})",
            "index_state": "rebuilding",
            "job_id": exc.job_id,
        }) from None
    except GraphSceneCapacityExceeded as exc:
        logger.info("graph scene exceeds capacity (%s, resource=%s, count=%s, limit=%s)",
                    type(exc).__name__, exc.resource, exc.count, exc.limit)
        raise HTTPException(status_code=413, detail={
            "error": "graph scene exceeds the safety limit",
            "safety_state": "capacity_exceeded",
            "degraded": True,
            "truncated": False,
            "resource": exc.resource,
            "count": exc.count,
            "limit": exc.limit,
            "recommended_action": "narrow repository, time, type, or relation filters",
        }) from None
    except ValidationError:
        logger.info("dashboard request rejected")
        raise _invalid_request() from None
    except HTTPException as exc:
        logger.info("dashboard dependency rejected request (status=%s)", exc.status_code)
        raise _sanitized_http_exception(exc.status_code) from None
    except Exception as exc:  # noqa: BLE001
        if _is_embedder_mismatch(exc):
            raise HTTPException(status_code=409, detail={
                "error": "Semantic search needs the embedding model that built your data "
                         "(sentence-transformers / all-MiniLM). Install it once — "
                         "pip install \"sentence-transformers>=2.7\" — then restart the "
                         "dashboard. The Memories, Graph, Overview and Audit tabs work without it.",
                "embedder": True})
        logger.error("dashboard operation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"error": "internal server error"})


#: What the dashboard shows when a managed failure carries no usable public copy.
_MANAGED_ERROR_FALLBACK = "managed cloud operation failed"
#: Hard ceiling on the forwarded copy. Every message this boundary forwards is fixed,
#: status-keyed public text (see ``_managed_error_message``), so nothing legitimate comes
#: close; a message that does is by definition not the fixed copy and is dropped.
_MANAGED_ERROR_MAX_CHARS = 300


def _managed_error_message(exc) -> str:
    """Return the public copy a ``CloudFeatureError`` carries, or the generic fallback.

    ``CloudFeatureError`` is the *already redacted* form: every raise site in
    ``cloud_features`` builds its message from fixed copy keyed on a status
    (``_public_http_error`` / ``_public_session_error``) or from a local literal. Provider
    bodies, ``CloudSessionError`` text and local state paths are all dropped before the
    exception is constructed, which is the whole point of that type.

    Flattening it again here cost the customer the only actionable part: a 429 or a 5xx
    ("temporarily busy", "temporarily unavailable" — retry) and a 409 ("could not accept the
    current workspace state" — do not retry, fix the session) all rendered as one fixed
    string, so the dashboard's error branch could not tell an outage from a conflict.

    The bound below is a boundary check, not the redaction: it keeps an unexpected message
    (a future raise site that interpolates something, or a subclass raised elsewhere) from
    becoming an unbounded, control-character-carrying string in a JSON error body.
    """

    message = " ".join(str(exc).split())
    if not message or len(message) > _MANAGED_ERROR_MAX_CHARS:
        return _MANAGED_ERROR_FALLBACK
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in message):
        return _MANAGED_ERROR_FALLBACK
    return message


def _managed_call(fn, *args, **kwargs):
    """Map the public cloud-client protocol onto structured dashboard errors."""
    from engraphis.cloud_features import CloudFeatureError

    try:
        return fn(*args, **kwargs)
    except CloudFeatureError as exc:
        logger.warning("managed cloud operation failed (%s, status=%s, transient=%s)",
                       type(exc).__name__, exc.status, exc.transient)
        detail = {"error": _managed_error_message(exc), "managed_cloud": True,
                  "transient": exc.transient}
        if exc.code in {"consent_required", "cloud_unconfigured"}:
            detail["code"] = exc.code
        raise HTTPException(
            status_code=exc.status or 503,
            detail=detail,
        ) from None


def _default_ws() -> Optional[str]:
    wss = service().list_workspaces().get("workspaces") or []
    return wss[0]["name"] if wss else None


def _require_ws(workspace: Optional[str] = None) -> str:
    """Resolve an explicitly selected workspace, with a legacy default fallback.

    Cloud automation is workspace-scoped.  Existing clients that omit the query
    parameter retain the historic first-workspace behavior, but an explicit value
    is cleaned and authorized before it is used.  Callers validate existence at
    the cloud boundary so an unknown selection receives the documented 404.
    """
    if workspace is not None:
        return service()._clean_ws(workspace)
    ws = _default_ws()
    if not ws:
        raise HTTPException(status_code=400, detail={"error": "No workspace exists yet. Create one first."})
    return ws


def _mem(m: dict) -> dict:
    """Normalize a v2 memory dict to the fields the dashboard cards render."""
    return {
        "id": m.get("id") or m.get("memory_id") or "",
        "document_id": m.get("id") or m.get("memory_id") or "",
        "title": m.get("title") or "",
        "content": m.get("content") or m.get("summary") or "",
        "memory_type": m.get("mtype") or "semantic",
        "scope": m.get("scope") or "",
        "namespace": m.get("workspace") or m.get("scope") or "",
        "score": m.get("score"),
        "relative_score": m.get("relative_score"),
        "absolute_support": m.get("absolute_support"),
        "arm": m.get("arm"),
        "retention": m.get("retention"),
        "pinned": bool(m.get("pinned", False)),
        "importance": m.get("importance"),
        "valid_from": m.get("valid_from"),
        "valid_to": m.get("valid_to"),
        "valid_to_recorded_at": m.get("valid_to_recorded_at"),
        "expired_at": m.get("expired_at"),
        "ingested_at": m.get("ingested_at"),
        "subject_key": m.get("subject_key") or "",
        "claim_kind": m.get("claim_kind") or "",
        "provenance": m.get("provenance") or {},
    }


def _is_embedder_mismatch(exc) -> bool:
    """Recognize the legacy vector-shape failure without returning its text."""
    message = str(exc)
    return "not aligned" in message or ("256" in message and "384" in message)


def _keyword_search(ws, q, limit=20, *, as_of: Optional[float] = None,
                    valid_at: Optional[float] = None,
                    known_at: Optional[float] = None):
    """Non-semantic fallback: match memories by keyword (title/content LIKE) so the
    Recall/Why/Timeline tabs still return results when the embedder is unavailable."""
    import json as _json
    import sqlite3 as _sql
    ws = service()._clean_ws(ws)
    conn = _sql.connect("file:%s?mode=ro" % settings.db_path, uri=True)
    conn.row_factory = _sql.Row
    try:
        row = conn.execute("SELECT id FROM workspaces WHERE name=?", (ws,)).fetchone()
        if row is None:
            return []
        # Match the public Recall contract even if semantic retrieval cannot run.
        # A model-dimension mismatch must degrade retrieval quality, never silently
        # turn a historical request into a present-time data leak.
        if as_of is not None and valid_at is not None and float(as_of) != float(valid_at):
            raise ValidationError("as_of and valid_at must match when both are supplied")
        world_anchor = float(valid_at if valid_at is not None else as_of) if (
            valid_at is not None or as_of is not None
        ) else time.time()
        system_anchor = float(known_at) if known_at is not None else time.time()
        sql = ("SELECT id, scope, mtype, title, content, summary, pinned, importance, "
               "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at, "
               "subject_key, claim_kind, provenance, metadata FROM memories "
               "WHERE workspace_id=? "
               "AND COALESCE(scope, 'workspace')!='session' "
               "AND (valid_from IS NULL OR valid_from<=?) "
               "AND (valid_to IS NULL OR ?<valid_to "
               "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at)) "
               "AND (ingested_at IS NULL OR ingested_at<=?) "
               "AND (expired_at IS NULL OR ?<expired_at)")
        args = [
            row["id"], world_anchor, world_anchor, system_anchor,
            system_anchor, system_anchor,
        ]
        terms = [t for t in (q or "").split() if len(t) > 2][:6]
        if terms:
            sql += " AND (" + " OR ".join(["title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'" for _ in terms]) + ")"
            for t in terms:
                args += ["%" + _escape_like(t) + "%", "%" + _escape_like(t) + "%"]
        sql += " ORDER BY COALESCE(last_access, valid_from) DESC"
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    def _prov(pp):
        try:
            return _json.loads(pp) if isinstance(pp, str) and pp else {}
        except Exception:  # noqa: BLE001
            return {}
    eligible = []
    for row in rows:
        provenance = _prov(row["provenance"])
        metadata = _prov(row["metadata"])
        if not prompt_eligible(provenance, metadata):
            continue
        eligible.append({
            "id": row["id"], "document_id": row["id"], "title": row["title"] or "",
            "content": row["content"] or row["summary"] or "",
            "memory_type": row["mtype"] or "semantic", "scope": row["scope"] or "",
            "pinned": bool(row["pinned"]), "importance": row["importance"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            "valid_to_recorded_at": row["valid_to_recorded_at"],
            "ingested_at": row["ingested_at"], "expired_at": row["expired_at"],
            "subject_key": row["subject_key"] or "", "claim_kind": row["claim_kind"] or "",
            "provenance": provenance,
        })
    # Do not cap the SQL candidates first: untrusted recent rows must not consume
    # the caller's result budget and hide eligible fallback evidence.
    requested = int(limit)
    return eligible if requested < 0 else eligible[:requested]


def _score_keyword_recall(query: str, memories: list[dict]) -> list[dict]:
    """Attach truthful lexical-only scores to degraded recall results.

    The fallback has no usable semantic embedder, so its absolute evidence signal is
    lexical Jaccard only. The relative compatibility score follows the normal recall
    contract by min-max normalizing within this response; ties retain the SQL fallback's
    recency order.
    """
    query_tokens = tokenize(query)
    absolute = {
        str(memory.get("id") or index): jaccard(
            query_tokens,
            tokenize(f"{memory.get('title') or ''}\n{memory.get('content') or ''}"),
        )
        for index, memory in enumerate(memories)
    }
    relative = normalize(absolute)
    scored = []
    for index, memory in enumerate(memories):
        key = str(memory.get("id") or index)
        item = dict(memory)
        item["score"] = round(relative.get(key, 0.0), 4)
        item["relative_score"] = item["score"]
        item["absolute_support"] = round(absolute.get(key, 0.0), 4)
        item["arm"] = "lexical"
        scored.append((index, item))
    scored.sort(key=lambda pair: (-pair[1]["relative_score"], pair[0]))
    return [item for _index, item in scored]


# ── health / bootstrap ────────────────────────────────────────────────────────
@router.get("")
def api_index():
    """Small, stable landing document for the API URL printed by the dashboard."""
    from engraphis import __version__
    return {
        "service": "engraphis",
        "version": __version__,
        "health": "/api/health",
        "ready": "/api/ready",
        "openapi": "/api/openapi.json",
    }


@router.get("/health")
def health():
    return {"status": "ok", "engine": "v2"}


@router.get("/bootstrap")
def bootstrap():
    lic = get_license()
    current_service = service()
    wss = _run(current_service.list_workspaces).get("workspaces") or []
    # A workspace-bound server rejects global aggregate statistics. Bootstrap must
    # first choose one of the already-authorized workspaces so the dashboard can
    # establish WS instead of failing before it renders the workspace switcher.
    scoped_stats_workspace = None
    if current_service.allowed_workspaces is not None and wss:
        scoped_stats_workspace = max(
            wss,
            key=lambda item: (int(item.get("memories") or 0), str(item.get("name") or "")),
        ).get("name")
    emb = None
    try:
        from engraphis.backends.embedder_deterministic import DeterministicEmbedder
        e = current_service.engine.embedder
        d = int(getattr(e, "dim", 0))
        semantic = not isinstance(e, DeterministicEmbedder)
        # ``LAST_EMBEDDER_ERROR`` may contain a provider exception. The bootstrap
        # contract exposes capability metadata only, never the failure text.
        emb = {"class": type(e).__name__, "dim": d, "semantic": semantic,
               "model": settings.embed_model}
    except Exception:  # noqa: BLE001
        pass
    return {
        "license": lic,
        "workspaces": wss,
        "stats": _run(current_service.stats, workspace=scoped_stats_workspace),
        "embedder": emb,
        # Non-blocking best-known update snapshot; the dashboard renders an "update
        # available" banner from this and a background refresh warms the cache.
        "update": _update_snapshot(),
    }


def _update_snapshot() -> dict:
    """Best-known update snapshot for the dashboard; never raises into bootstrap."""
    try:
        from engraphis import update_check
        return update_check.snapshot()
    except Exception:  # noqa: BLE001 - a convenience feature must not break bootstrap
        return {"enabled": False, "update_available": False}


@router.get("/update")
def api_update(force: bool = False):
    """Update-availability snapshot for the dashboard banner.

    Cached ~24h and fail-silent: reports the newest published release vs the installed
    version. ``?force=1`` bypasses the cache and re-checks now.
    """
    try:
        from engraphis import update_check
        return update_check.check(force=True) if force else update_check.snapshot()
    except Exception:  # noqa: BLE001
        return {"enabled": False, "update_available": False}


# ── workspaces / stats ────────────────────────────────────────────────────────
@router.get("/workspaces")
def workspaces():
    return _run(service().list_workspaces)


# ── LLM connection status + test (dashboard "Connect your LLM" card) ───────────

# Provider → sensible default model, so the dashboard's provider picker can prefill a
# working model name without the user needing to know the provider's catalogue.
_LLM_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "google": "gemini-1.5-flash",
    "openrouter": "openai/gpt-4o-mini",
}

_llm_connection_state: dict = {
    "ok": False,
    "provider": "",
    "model": "",
    "tested_at": 0.0,
}
_LLM_PUBLIC_TEST_STATE_FIELDS = (
    "extractor",
    "extractor_enabled",
    "auto_extract",
    "auto_enabled",
    "persisted",
)
_llm_extractor_lock = threading.RLock()
_sync_token_state_lock = threading.RLock()


class _DisabledExtractor:
    """A race-safe equivalent of ``extractor=None`` for live configuration changes.

    ``MemoryEngine.ingest`` checks the attribute and then reads it again for ``extract``.
    Replacing a live extractor with ``None`` between those reads can raise AttributeError;
    a stable no-op object instead returns no facts and lets the engine use its normal
    passthrough fallback.
    """

    @staticmethod
    def extract(text: str, *, context: str = "") -> list:
        return []


_DISABLED_EXTRACTOR = _DisabledExtractor()


def _extractor_enabled() -> bool:
    from engraphis.backends.extractor import LLMExtractor, StructuredLLMExtractor
    return isinstance(service().engine.extractor, (LLMExtractor, StructuredLLMExtractor))


def _close_llm(llm) -> None:
    if llm is not None and hasattr(llm, "close"):
        try:
            llm.close()
        except Exception:  # noqa: BLE001 - cleanup cannot block a settings change
            pass


def _retire_extractor(extractor) -> None:
    """Close an old extractor only after every in-flight request releases it."""
    llm = getattr(extractor, "llm", None)
    if llm is None or not hasattr(llm, "close"):
        return
    try:
        weakref.finalize(extractor, _close_llm, llm)
    except TypeError:
        # A non-weak-referenceable third-party extractor cannot be closed safely here:
        # another request may still be using it. Prefer a bounded, rare resource leak to
        # terminating an in-flight provider call.
        logger.warning("retired LLM extractor could not be finalized safely")


def _set_llm_extractor(enabled: bool, *, persist: bool = True) -> dict:
    """Apply the dashboard extractor switch immediately and, when possible, durably."""
    with _llm_extractor_lock:
        return _set_llm_extractor_locked(enabled, persist=persist)


def _set_llm_extractor_locked(enabled: bool, *, persist: bool) -> dict:
    old = service().engine.extractor
    if enabled:
        from engraphis.backends.extractor import PassthroughExtractor, get_extractor
        new = get_extractor("llm_structured")
        if isinstance(new, PassthroughExtractor):
            raise RuntimeError("structured LLM extractor could not be initialized")
        service().engine.extractor = new
        extractor = "llm_structured"
    else:
        service().engine.extractor = _DISABLED_EXTRACTOR
        extractor = "none"
    if old is not service().engine.extractor:
        _retire_extractor(old)

    settings.extractor = extractor
    settings.llm_auto_extract = bool(enabled)
    os.environ["ENGRAPHIS_EXTRACTOR"] = extractor
    os.environ["ENGRAPHIS_LLM_AUTO_EXTRACT"] = "1" if enabled else "0"
    persisted = False
    if persist:
        try:
            from engraphis.config import persist_project_env
            persist_project_env({
                "ENGRAPHIS_EXTRACTOR": extractor,
                "ENGRAPHIS_LLM_AUTO_EXTRACT": "1" if enabled else "0",
            })
            persisted = True
        except (OSError, ValueError) as exc:
            logger.warning("could not persist LLM extractor setting (%s)", type(exc).__name__)
    return {
        "extractor": extractor,
        "extractor_enabled": bool(enabled),
        "auto_extract": bool(enabled),
        "persisted": persisted,
    }


def _record_llm_test(result: dict) -> None:
    provider = settings.llm_provider or "openai"
    model = settings.llm_model or _LLM_DEFAULT_MODELS.get(provider, "")
    with _llm_extractor_lock:
        _llm_connection_state.update({
            "ok": bool(result.get("ok")),
            "provider": provider,
            "model": model,
            "tested_at": time.time(),
        })


def _public_llm_test_result(result: dict, *, error: str = "") -> dict:
    """Return the strict HTTP allow-list for an LLM connection test.

    Provider replies and arbitrary adapter fields are untrusted external payloads. They
    may be useful inside the process, but must never cross the dashboard API boundary.
    """
    result = result if isinstance(result, dict) else {}
    provider = settings.llm_provider or "openai"
    model = settings.llm_model or _LLM_DEFAULT_MODELS.get(provider, "")
    ok = bool(result.get("ok"))
    public = {"ok": ok, "provider": provider, "model": model}
    for field in _LLM_PUBLIC_TEST_STATE_FIELDS:
        if field in result:
            public[field] = result[field]
    if not ok:
        public["error"] = error or (
            "The provider test failed. Check the configured provider, model, API key, "
            "and network connection."
        )
    return public


def _llm_is_verified(provider: str, model: str) -> bool:
    with _llm_extractor_lock:
        return bool(
            _llm_connection_state.get("ok")
            and _llm_connection_state.get("provider") == provider
            and _llm_connection_state.get("model") == model
        )


@router.get("/llm/status")
def llm_status():
    """Report the configured LLM provider/model/key presence and the active extractor,
    retention-supervision mode, and a ready-to-paste .env snippet for the dashboard's
    "Connect your LLM" card. Never returns the API key or custom provider endpoint —
    only whether each is set."""
    provider = settings.llm_provider or "openai"
    model = settings.llm_model or _LLM_DEFAULT_MODELS.get(provider, "")
    key_set = bool(settings.llm_api_key)
    verified = bool(key_set and _llm_is_verified(provider, model))
    return {
        "provider": provider,
        "model": model,
        "key_set": key_set,
        "custom_base_url_configured": bool(settings.llm_base_url),
        "extractor": settings.extractor,
        "extractor_enabled": _extractor_enabled(),
        "retention_supervisor": settings.retention_supervisor,
        "auto_extract": bool(settings.llm_auto_extract),
        "configured": key_set and bool(model),
        "working": verified,
        "tested_at": (_llm_connection_state.get("tested_at") if verified else 0.0),
        "default_models": _LLM_DEFAULT_MODELS,
        # A copy-paste .env block so the user doesn't have to memorise var names.
        "env_snippet": (
            f"ENGRAPHIS_LLM_PROVIDER={provider}\n"
            f"ENGRAPHIS_LLM_MODEL={model}\n"
            f"ENGRAPHIS_LLM_API_KEY=<your-key>\n"
            + ("ENGRAPHIS_EXTRACTOR=llm_structured\n" if key_set else "# set ENGRAPHIS_EXTRACTOR=llm_structured to use it\n")
            + "ENGRAPHIS_LLM_AUTO_EXTRACT=1\n"
        ),
    }


@router.post("/llm/test")
def llm_test():
    """Live-test the configured LLM with a tiny completion. POST so the dashboard auth
    gate (member+ in team mode) applies — testing spends a fraction of a cent of the
    instance's API credit, so it's not a viewer action. Returns an allow-listed status,
    never the provider reply; failures are mapped to a generic error."""
    if not settings.llm_api_key:
        _record_llm_test({"ok": False})
        return _public_llm_test_result(
            {"ok": False},
            error=("No API key configured. Set ENGRAPHIS_LLM_API_KEY in your .env "
                   "and restart."),
        )
    try:
        from engraphis.llm.client import LLMClient
        with LLMClient() as llm:
            result = llm.ping()
        result = result if isinstance(result, dict) else {"ok": False}
        _record_llm_test(result)
        if result.get("ok") and settings.llm_auto_extract:
            result.update(_set_llm_extractor(True))
            result["auto_enabled"] = True
        else:
            result.update({
                "extractor": settings.extractor,
                "extractor_enabled": _extractor_enabled(),
                "auto_extract": bool(settings.llm_auto_extract),
                "auto_enabled": False,
            })
        return _public_llm_test_result(result)
    except Exception as exc:  # noqa: BLE001
        _record_llm_test({"ok": False})
        logger.error("LLM connection test failed (%s)", type(exc).__name__)
        return _public_llm_test_result({"ok": False})


class _ExtractorToggleReq(BaseModel):
    enabled: bool


@router.post("/llm/extractor")
def llm_extractor_toggle(req: _ExtractorToggleReq):
    """Turn structured extraction on/off immediately; enabling requires a live provider."""
    if not req.enabled:
        return {"ok": True, **_set_llm_extractor(False)}
    if not settings.llm_api_key:
        raise HTTPException(status_code=400, detail={
            "error": "Connect an LLM and set its API key before enabling extraction."})
    try:
        from engraphis.llm.client import LLMClient
        with LLMClient() as llm:
            result = llm.ping()
    except Exception as exc:  # noqa: BLE001 - provider clients fail in many library-specific ways
        _record_llm_test({"ok": False})
        logger.error("LLM extractor verification failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=400, detail={
            "error": "The configured LLM could not be verified. Check the provider, "
                     "model, API key, and network connection."}) from None
    result = result if isinstance(result, dict) else {"ok": False}
    _record_llm_test(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail={
            "error": "The configured LLM could not be verified. Check the provider, "
                     "model, API key, and network connection."})
    return {"ok": True, "provider": settings.llm_provider,
            "model": settings.llm_model, **_set_llm_extractor(True)}


def _metadata_object(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, RecursionError):
        return {}


@router.get("/llm/activity")
def llm_activity(workspace: Optional[str] = None, limit: int = 100):
    """List memories the LLM extracted, consolidated, or retention-classified.

    This is intentionally a derived audit view: it exposes stored memory outcomes and
    bounded metadata, never prompts, API keys, or raw provider responses.
    """
    ws = workspace or _default_ws()
    if not ws:
        return {"workspace": "", "count": 0, "activities": []}
    try:
        ws = service()._clean_ws(ws)
    except ValidationError:
        logger.info("LLM activity request rejected")
        raise _invalid_request() from None
    row = service().store.conn.execute(
        "SELECT id FROM workspaces WHERE name=?", (ws,)
    ).fetchone()
    if row is None:
        return {"workspace": ws, "count": 0, "activities": []}
    rows = service().store.conn.execute(
        "SELECT id, title, content, mtype, ingested_at, metadata FROM memories "
        "WHERE workspace_id=? AND COALESCE(scope, 'workspace')!='session' "
        "AND valid_to IS NULL AND expired_at IS NULL AND ("
        "metadata LIKE '%\"llm_extraction\"%' OR "
        "metadata LIKE '%\"structured_extraction\"%' OR "
        "metadata LIKE '%\"structured_consolidation\"%' OR "
        "metadata LIKE '%\"retention_supervision\"%') "
        "ORDER BY ingested_at DESC LIMIT ?",
        (row["id"], max(1, min(500, int(limit)))),
    ).fetchall()
    activities = []
    for record in rows:
        metadata = _metadata_object(record["metadata"])
        extraction = metadata.get("llm_extraction")
        consolidation = metadata.get("structured_consolidation")
        retention = metadata.get("retention_supervision")
        if isinstance(extraction, dict):
            action = "extracted"
            detail = extraction
        elif isinstance(consolidation, dict):
            action = "consolidated"
            detail = consolidation
        elif isinstance(retention, dict) and retention.get("source") == "llm":
            action = "retention supervised"
            detail = retention
        elif isinstance(metadata.get("structured_extraction"), dict):
            action = "extracted"
            detail = {"mode": "llm_structured", "legacy": True}
        else:
            continue
        structured = metadata.get("structured_extraction") or {}
        entities = metadata.get("entities") or structured.get("entities") or []
        relations = metadata.get("relations") or structured.get("relations") or []
        activities.append({
            "id": record["id"],
            "title": record["title"] or "",
            "content": record["content"] or "",
            "mtype": record["mtype"] or "semantic",
            "ingested_at": record["ingested_at"],
            "action": action,
            "provider": detail.get("provider") or "",
            "model": detail.get("model") or "",
            "mode": detail.get("mode") or "",
            "fact_index": detail.get("fact_index"),
            "fact_count": detail.get("fact_count"),
            "confidence": detail.get("confidence", structured.get("confidence")),
            "entities": entities[:20] if isinstance(entities, list) else [],
            "relations": relations[:10] if isinstance(relations, list) else [],
            "source_count": detail.get("source_count"),
        })
    return {"workspace": ws, "count": len(activities), "activities": activities}


class _CreateWsReq(BaseModel):
    workspace: str
    description: str = ""
    visibility: str = "personal"
    confirmed: bool = False


@router.post("/workspaces/create")
def workspaces_create(req: _CreateWsReq):
    """Create an empty workspace/folder up front (see MemoryService.create_workspace).
    In team mode this is a POST, so the dashboard's auth gate requires the member role or
    above — viewers can't create folders, members and admins can. New folders are personal
    by default. A shared folder needs an explicit ``visibility='shared'`` request plus
    ``confirmed=true``; the owner is taken from the session, never from the request body."""
    return _run(service().create_workspace, req.workspace, req.description,
                visibility=req.visibility, confirmed=req.confirmed)


class _WorkspaceVisibilityReq(BaseModel):
    workspace: str
    visibility: str
    confirmed: bool = False


@router.post("/workspaces/visibility")
def workspaces_visibility(req: _WorkspaceVisibilityReq):
    """Change folder sharing only after the caller explicitly confirms it."""
    return _run(service().set_workspace_visibility, req.workspace, req.visibility,
                confirmed=req.confirmed)


class _RenameWsReq(BaseModel):
    workspace: str
    new_name: str


@router.post("/workspaces/rename")
def workspaces_rename(req: _RenameWsReq):
    return _run(service().rename_workspace, req.workspace, req.new_name)


class _DescribeWsReq(BaseModel):
    workspace: str
    description: str = ""


@router.post("/workspaces/describe")
def workspaces_describe(req: _DescribeWsReq):
    return _run(service().set_workspace_description, req.workspace, req.description)


class _CopyWsReq(BaseModel):
    workspace: str
    new_name: Optional[str] = None


@router.post("/workspaces/copy")
def workspaces_copy(req: _CopyWsReq):
    """Duplicate ``workspace`` into a new one (see MemoryService.copy_workspace). When
    ``new_name`` is omitted the name is auto-generated so the dashboard's Copy button
    is a single click."""
    return _run(service().copy_workspace, req.workspace, req.new_name)


class _DeleteWsReq(BaseModel):
    workspace: str


@router.post("/workspaces/delete")
def workspaces_delete(req: _DeleteWsReq):
    return _run(service().delete_workspace, req.workspace)


class _MergeWsReq(BaseModel):
    source: str
    target: str


@router.post("/workspaces/merge")
def workspaces_merge(req: _MergeWsReq):
    """Fold ``source`` workspace into ``target`` (lossless move, see MemoryService.merge_workspaces)."""
    return _run(service().merge_workspaces, req.source, req.target)


class _ImportFolderReq(BaseModel):
    workspace: str
    path: str
    file_pattern: str = "*.md"
    memory_type: str = "semantic"
    derive_facts: bool = False


@router.post("/workspaces/import-folder")
def workspaces_import_folder(req: _ImportFolderReq):
    """Import files from a directory on the machine running Engraphis into ``workspace``,
    one memory per file (see MemoryService.import_folder, SECURITY.md §5). Team mode
    restricts this server-local filesystem read to administrators; the allowlisted-root
    traversal guard itself lives in the service layer."""
    return _run(service().import_folder, workspace=req.workspace, path=req.path,
                file_pattern=req.file_pattern, memory_type=req.memory_type,
                derive_facts=req.derive_facts)


@router.post("/workspaces/import-files")
async def workspaces_import_files(workspace: str = Form(...),
                                  memory_type: str = Form("semantic"),
                                  derive_facts: bool = Form(False),
                                  files: list[UploadFile] = File(...)):
    """Drag-and-drop / picked-file upload counterpart to import-folder (see
    MemoryService.import_files). Each upload is read bounded by
    ``MemoryService.MAX_IMPORT_RESOURCE_BYTES`` — a resource bound, not a
    security boundary (see that constant's docstring); the rest of validation is
    transport-agnostic and lives in the service layer, same as every other write."""
    from engraphis.service import (
        MAX_IMPORT_FILES,
        MAX_IMPORT_RESOURCE_BYTES,
        MAX_IMPORT_TOTAL_BYTES,
    )
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail={
            "error": f"too many files (max {MAX_IMPORT_FILES})"
        })
    payload = []
    total = 0
    for f in files:
        remaining = MAX_IMPORT_TOTAL_BYTES - total
        raw = await f.read(min(MAX_IMPORT_RESOURCE_BYTES, max(0, remaining)) + 1)
        if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
            raise HTTPException(status_code=413, detail={
                "error": f"{f.filename or 'file'} is too large"
            })
        if len(raw) > remaining:
            raise HTTPException(status_code=413, detail={
                "error": f"upload batch exceeds {MAX_IMPORT_TOTAL_BYTES} bytes"
            })
        total += len(raw)
        payload.append({"name": f.filename or "untitled",
                        "data": raw})
    return _run(service().import_files, workspace=workspace, files=payload,
                memory_type=memory_type, derive_facts=derive_facts)


class _PostgresImportReq(BaseModel):
    dsn: str
    workspace: str
    repo: Optional[str] = None
    schemas: Optional[list[str]] = None


@router.post("/resources/postgres")
def resources_postgres(req: _PostgresImportReq):
    return _run(
        service().import_postgres_schema, req.dsn, workspace=req.workspace,
        repo=req.repo, schemas=req.schemas,
    )


class _UpdateMemReq(BaseModel):
    id: str
    workspace: Optional[str] = None
    title: Optional[str] = None
    memory_type: Optional[str] = None
    importance: Optional[float] = None


@router.post("/memory/update")
def memory_update(req: _UpdateMemReq):
    ws = req.workspace or _default_ws()
    return _run(service().update_memory, req.id, workspace=ws,
                title=req.title, mtype=req.memory_type, importance=req.importance)


class _ReorderReq(BaseModel):
    ids: list[str]
    workspace: Optional[str] = None
    repo: Optional[str] = None


@router.post("/memories/reorder")
def memories_reorder(req: _ReorderReq):
    """Persist the Memories tab's drag-to-reorder position for a full id list."""
    ws = req.workspace or _default_ws()
    return _run(service().reorder_memories, req.ids, workspace=ws, repo=req.repo)


@router.get("/stats")
def stats(workspace: Optional[str] = None):
    return _run(service().stats, workspace=workspace)


# ── recall / search ───────────────────────────────────────────────────────────
@router.get("/recall")
def recall(q: str = Query(...), workspace: Optional[str] = None, k: int = 8,
           mtype: Optional[str] = None, as_of: Optional[float] = None,
           valid_at: Optional[float] = None, known_at: Optional[float] = None,
           token_budget: Optional[int] = Query(default=None, ge=0, le=32_768),
           retrieval_profile: str = "balanced", candidate_depth: str = "fixed",
           response_mode: str = "full",
           diagnostics: bool = False):
    ws = workspace or _default_ws()
    mtypes = [mtype] if mtype else None
    try:
        out = service().recall(
            q, workspace=ws, k=k, mtypes=mtypes, as_of=as_of,
            valid_at=valid_at, known_at=known_at, reinforce=False,
            token_budget=token_budget, retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            response_mode=response_mode, diagnostics=diagnostics,
        )
    except ValidationError:
        logger.info("dashboard recall request rejected")
        raise _invalid_request() from None
    except Exception as exc:  # noqa: BLE001
        if not _is_embedder_mismatch(exc):
            logger.error("dashboard recall failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail={"error": "internal server error"})
        mems = _keyword_search(
            ws, q, k, as_of=as_of, valid_at=valid_at, known_at=known_at,
        )
        mems = _score_keyword_recall(q, mems)
        if response_mode == "compact":
            # Preserve the public compact-response contract even when semantic recall
            # degrades to the keyword path during an embedding migration.
            mems = [
                {
                    key: memory.get(key)
                    for key in (
                        "id", "document_id", "title", "memory_type", "scope", "pinned",
                        "score", "relative_score", "absolute_support", "arm",
                        "importance", "valid_from", "valid_to", "valid_to_recorded_at",
                        "ingested_at", "expired_at", "subject_key", "claim_kind", "provenance",
                    )
                }
                for memory in mems
            ]
        historical = valid_at is not None or known_at is not None or as_of is not None
        effective_budget = (
            token_budget if token_budget is not None else service().engine.recall_engine.token_budget
        )
        return {"query": q, "workspace": ws, "count": len(mems), "context": "",
                "memories": mems, "mode": "keyword", "response_mode": response_mode,
                "retrieval_profile": retrieval_profile,
                "candidate_depth": candidate_depth,
                "candidate_k_requested": 50, "candidate_k_used": 0,
                "candidate_depth_reason": "keyword fallback",
                "score_semantics": dict(_KEYWORD_SCORE_SEMANTICS),
                "valid_at": valid_at if valid_at is not None else as_of,
                "known_at": known_at, "historical": historical,
                "packed_sources": [],
                "usage": {"budget_tokens": effective_budget, "context_tokens": 0,
                          "source_tokens": 0, "saved_tokens": 0, "savings_ratio": 0.0,
                          "packed_count": 0, "omitted_count": len(mems),
                          "token_counter": "engraphis.regex.v1"},
                "note": "Keyword match — install sentence-transformers for semantic search."}
    payload = dict(out)
    payload.update({
        "query": q,
        "workspace": ws,
        "count": out.get("count", 0),
        "context": out.get("context", ""),
        "mode": "semantic",
        "memories": [_mem(m) for m in out.get("memories", [])],
    })
    return payload


class _AnswerReq(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    workspace: Optional[str] = None
    repo: Optional[str] = None
    k: int = Field(default=8, ge=1, le=50)
    max_citations: int = Field(default=5, ge=1, le=50)
    min_support: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    token_budget: Optional[int] = Field(default=None, ge=0, le=32_768)
    retrieval_profile: str = "balanced"
    candidate_depth: str = "fixed"
    response_mode: str = "full"
    diagnostics: bool = False


@router.post("/answer")
def answer(req: _AnswerReq):
    """Return a strictly grounded answer with citations, or explicitly abstain.

    Ledger uses this route instead of presenting nearest-neighbour search results as an
    answer. The service owns support scoring, workspace isolation, reinforcement and the
    privacy-safe operation receipt; this adapter only applies the dashboard's bounded
    request model and stable error boundary.
    """
    ws = req.workspace or _default_ws()
    out = _run(
        service().grounded_recall,
        req.query,
        workspace=ws,
        repo=req.repo,
        k=req.k,
        as_of=req.as_of,
        valid_at=req.valid_at,
        known_at=req.known_at,
        token_budget=req.token_budget,
        retrieval_profile=req.retrieval_profile,
        candidate_depth=req.candidate_depth,
        response_mode=req.response_mode,
        diagnostics=req.diagnostics,
        max_citations=req.max_citations,
        min_support=req.min_support,
    )
    out["sources"] = list(out.get("citations") or [])
    return out


@router.get("/memories")
def memories(workspace: Optional[str] = None, q: Optional[str] = None, limit: int = 200):
    """List memories directly from the store (no embedding) so browsing works even
    without sentence-transformers. Live memories only (not superseded/expired)."""
    import json as _json
    import sqlite3 as _sql
    ws = workspace or _default_ws()
    if not ws:
        # No workspace exists yet (fresh install) — nothing to list. Return an empty
        # result instead of letting _clean_ws(None) raise a 500.
        return {"workspace": "", "count": 0, "memories": []}
    try:
        ws = service()._clean_ws(ws)
    except ValidationError:
        logger.info("dashboard memories request rejected")
        raise _invalid_request() from None
    conn = _sql.connect("file:%s?mode=ro" % settings.db_path, uri=True)
    conn.row_factory = _sql.Row
    try:
        row = conn.execute("SELECT id FROM workspaces WHERE name=?", (ws,)).fetchone()
        if row is None:
            return {"workspace": ws, "count": 0, "memories": []}
        sql = ("SELECT id, scope, mtype, title, content, summary, importance, pinned, "
               "valid_from, valid_to, provenance FROM memories WHERE workspace_id=? "
               "AND COALESCE(scope, 'workspace')!='session' "
               "AND valid_to IS NULL AND expired_at IS NULL")
        args = [row["id"]]
        if q:
            sql += " AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
            like = "%" + _escape_like(q) + "%"
            args += [like, like]
        # Manually dragged rows (sort_order set) come first, in the order they were
        # dropped in; everything never touched by drag-to-reorder falls back to recency.
        sql += " ORDER BY (sort_order IS NULL), sort_order ASC, COALESCE(last_access, valid_from) DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit))))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    def _prov(p):
        try:
            return _json.loads(p) if isinstance(p, str) and p else (p or {})
        except Exception:  # noqa: BLE001
            return {}
    mems = [{"id": r["id"], "document_id": r["id"], "title": r["title"] or "",
             "content": r["content"] or r["summary"] or "", "memory_type": r["mtype"] or "semantic",
             "scope": r["scope"] or "", "pinned": bool(r["pinned"]),
             "importance": r["importance"], "valid_from": r["valid_from"],
             "valid_to": r["valid_to"], "provenance": _prov(r["provenance"])} for r in rows]
    return {"workspace": ws, "count": len(mems), "memories": mems}


@router.get("/memory/{memory_id}")
def memory_detail(memory_id: str, workspace: Optional[str] = None):
    ws = workspace or _default_ws()
    out = _run(service().inspect, memory_id, workspace=ws)
    mem = out.get("memory") or {}
    return {"memory": _mem(mem) if mem else None,
            "chain": [_mem(m) for m in (out.get("chain") or [])],
            "links": out.get("links") or [], "audit": out.get("audit") or []}


# ── bi-temporal: why / timeline / proactive ──────────────────────────────────
@router.get("/why")
def why(q: str = Query(...), workspace: Optional[str] = None, k: int = 5):
    ws = workspace or _require_ws()
    try:
        out = service().why(q, workspace=ws, k=k)
    except ValidationError:
        logger.info("dashboard why request rejected")
        raise _invalid_request() from None
    except Exception as exc:  # noqa: BLE001
        if not _is_embedder_mismatch(exc):
            logger.error("dashboard why failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail={"error": "internal server error"})
        mems = _keyword_search(ws, q, k)
        return {"query": q, "workspace": ws, "answer": mems, "supersedes": [],
                "mode": "keyword",
                "note": "Keyword match — install sentence-transformers for semantic search."}
    return {"query": q, "workspace": ws, "mode": "semantic",
            "answer": [_mem(m) for m in out.get("answer", [])],
            "supersedes": [_mem(m) for m in out.get("supersedes", [])]}


@router.get("/timeline")
def timeline(q: str = Query(...), workspace: Optional[str] = None, limit: int = 20):
    ws = workspace or _default_ws()
    try:
        out = service().timeline(q, workspace=ws, limit=limit)
    except ValidationError:
        logger.info("dashboard timeline request rejected")
        raise _invalid_request() from None
    except Exception as exc:  # noqa: BLE001
        if not _is_embedder_mismatch(exc):
            logger.error("dashboard timeline failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail={"error": "internal server error"})
        mems = _keyword_search(ws, q, limit)
        return {"query": q, "workspace": ws, "history": mems, "mode": "keyword",
                "note": "Keyword match — install sentence-transformers for semantic search."}
    return {"query": q, "workspace": ws, "mode": "semantic",
            "history": [_mem(m) for m in out.get("history", [])]}


@router.get("/proactive")
def proactive(workspace: Optional[str] = None, k: int = 10):
    ws = workspace or _default_ws()
    out = _run(service().recall_proactive, workspace=ws, k=k)
    mems = out.get("memories") or out.get("results") or []
    return {"workspace": ws, "memories": [_mem(m) for m in mems],
            "handoff": out.get("handoff") or out.get("last_session")}


class _ProactiveContextReq(BaseModel):
    workspace: Optional[str] = None
    repo: Optional[str] = None
    task: str = ""
    agent_state: str = ""
    k: int = 10
    synthesize: bool = False


@router.post("/proactive-context")
def proactive_context(req: _ProactiveContextReq):
    ws = req.workspace or _default_ws()
    return _run(service().proactive_context, workspace=ws, repo=req.repo,
                task=req.task, agent_state=req.agent_state, k=req.k,
                synthesize=req.synthesize)


@router.get("/audit")
def audit(workspace: Optional[str] = None, limit: int = 100):
    ws = workspace or _require_ws()
    return _run(service().audit_log, workspace=ws, limit=limit)


@router.get("/receipts")
def receipts(workspace: Optional[str] = None, limit: int = 100):
    ws = workspace or _require_ws()
    return _run(service().receipt_log, workspace=ws, limit=limit)


@router.get("/context-savings")
def context_savings(workspace: Optional[str] = None, repo: Optional[str] = None):
    ws = workspace or _require_ws()
    return _run(service().context_savings, workspace=ws, repo=repo)


@router.get("/receipts/verify")
def receipts_verify(workspace: Optional[str] = None, expected_head: str = "",
                    expected_count: Optional[int] = None):
    ws = workspace or _require_ws()
    return _run(
        service().verify_receipts, workspace=ws,
        expected_head=expected_head, expected_count=expected_count,
    )


@router.get("/receipts/export")
def receipts_export(workspace: Optional[str] = None):
    ws = workspace or _require_ws()
    from fastapi.responses import JSONResponse
    body = _run(service().export_receipts, workspace=ws)
    fname = "engraphis-receipts-%s-%s.json" % (
        (ws or "workspace").replace("/", "_"),
        __import__("time").strftime("%Y%m%d"),
    )
    return JSONResponse(body, headers={
        "Content-Disposition": 'attachment; filename="%s"' % fname,
    })


# ── governance: pin / forget / correct ───────────────────────────────────────
class _IdReq(BaseModel):
    id: str
    workspace: Optional[str] = None
    repo: Optional[str] = None
    reason: str = ""
    pinned: bool = True
    content: str = ""
    target_scope: str = ""


@router.post("/pin")
def pin(req: _IdReq):
    ws = req.workspace or _default_ws()
    return _run(service().pin, req.id, workspace=ws, pinned=req.pinned)


@router.post("/forget")
def forget(req: _IdReq):
    ws = req.workspace or _default_ws()
    return _run(service().forget, req.id, workspace=ws, reason=req.reason)


@router.post("/correct")
def correct(req: _IdReq):
    ws = req.workspace or _default_ws()
    return _run(service().correct, req.id, req.content, workspace=ws, reason=req.reason)


@router.post("/promote")
def promote(req: _IdReq):
    ws = req.workspace or _default_ws()
    return _run(
        service().promote, req.id, req.target_scope, workspace=ws,
        repo=req.repo, reason=req.reason,
    )


class _MergeReq(BaseModel):
    ids: list[str]
    content: str
    workspace: Optional[str] = None
    title: Optional[str] = None
    memory_type: Optional[str] = None
    reason: str = "merged in dashboard"


@router.post("/merge")
def merge(req: _MergeReq):
    """Merge several selected memories into one (manual N→1). The sources are retired
    into history (bi-temporally closed, never hard-deleted) and the new memory
    supersedes them — the multi-input sibling of /correct. Validation, workspace
    authorization, and the safety inheritance rules all live in MemoryService.merge."""
    ws = req.workspace or _default_ws()
    return _run(service().merge, req.ids, req.content, workspace=ws,
                title=req.title, mtype=req.memory_type, reason=req.reason)


# ── agent connect (Team) ───────────────────────────────────────────────────────
# This is the single-user local agent write path. The optional deployment token is
# enforced by dashboard_app; hosted members, roles, seats, and remote agents live in Cloud.
class _RememberReq(BaseModel):
    content: str
    workspace: str = "default"
    repo: Optional[str] = None
    mtype: str = "semantic"
    scope: Optional[str] = None
    title: str = ""
    importance: float = 0.0
    keywords: Optional[list] = None
    metadata: Optional[dict] = None
    source: str = "agent"
    trusted: bool = True
    dedupe: bool = True
    retention_class: Optional[str] = None
    retention_reason: str = ""
    valid_from: Optional[float] = None
    subject_key: str = ""
    claim_kind: str = ""


@router.post("/remember")
def remember(req: _RememberReq):
    return _run(service().remember, req.content, workspace=req.workspace,
                repo=req.repo, mtype=req.mtype, scope=req.scope, title=req.title,
                importance=req.importance, keywords=req.keywords, metadata=req.metadata,
                # This authenticated local API is the customer node's normal write
                # surface. Callers can explicitly mark imported/external material
                # untrusted; that path remains quarantined and ineligible for
                # grounded answers without disabling ordinary local memory writes.
                source=req.source, trusted=req.trusted, resolve_conflicts=req.dedupe,
                retention_class=req.retention_class,
                retention_reason=req.retention_reason,
                valid_from=req.valid_from,
                subject_key=req.subject_key, claim_kind=req.claim_kind)


class _IntentRememberReq(BaseModel):
    text: str
    workspace: str = "default"
    repo: Optional[str] = None
    title: str = ""
    mtype: str = "semantic"
    scope: Optional[str] = None
    importance: float = 0.0
    metadata: Optional[dict] = None
    retention_class: Optional[str] = None
    retention_reason: str = ""
    valid_from: Optional[float] = None
    subject_key: str = ""
    claim_kind: str = ""


@router.post("/intent/remember")
def intent_remember(req: _IntentRememberReq):
    # The local open core accepts its own writes. Hosted remote-agent authorization is a
    # separate server-side Team boundary and is not implemented in this package.
    return _run(
        service().intent_remember, req.text, workspace=req.workspace, repo=req.repo,
        title=req.title, mtype=req.mtype, scope=req.scope, importance=req.importance,
        metadata=req.metadata, retention_class=req.retention_class,
        retention_reason=req.retention_reason,
        valid_from=req.valid_from,
        subject_key=req.subject_key, claim_kind=req.claim_kind,
    )


class _IntentLinkReq(BaseModel):
    source_id: str
    target_id: str
    workspace: str
    repo: Optional[str] = None
    relation: str = "related"
    layer: Optional[str] = None
    reason: str = ""


@router.post("/intent/link")
def intent_link(req: _IntentLinkReq):
    # Local link creation has no client-side commercial gate.
    return _run(
        service().intent_link, req.source_id, req.target_id, workspace=req.workspace,
        repo=req.repo, relation=req.relation, layer=req.layer, reason=req.reason,
    )


class _IntentRecallReq(BaseModel):
    query: str
    intent: str = "recall"
    workspace: Optional[str] = None
    repo: Optional[str] = None
    mtypes: Optional[list] = None
    k: int = 8
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    token_budget: Optional[int] = Field(default=None, ge=0, le=32_768)
    retrieval_profile: str = "balanced"
    candidate_depth: str = "fixed"
    response_mode: str = "compact"
    diagnostics: bool = False


@router.post("/intent/recall")
def intent_recall(req: _IntentRecallReq):
    return _run(
        service().intent_recall, req.query, intent=req.intent,
        workspace=req.workspace or _default_ws(), repo=req.repo,
        mtypes=req.mtypes, k=req.k, as_of=req.as_of,
        valid_at=req.valid_at, known_at=req.known_at,
        token_budget=req.token_budget, retrieval_profile=req.retrieval_profile,
        candidate_depth=req.candidate_depth,
        response_mode=req.response_mode, diagnostics=req.diagnostics,
    )


# ── consolidate ───────────────────────────────────────────────────────────────
class _ConsolidateReq(BaseModel):
    workspace: Optional[str] = None
    dry_run: bool = True
    infer: bool = False
    structured: bool = False
    supersede_sources: bool = False


@router.post("/consolidate")
def consolidate(req: _ConsolidateReq):
    if req.infer:
        raise HTTPException(status_code=501, detail={
            "error": "Dream inference is available through Engraphis Cloud managed compute.",
            "cloud_only": True,
            "feature": "automation",
            "upgrade_url": licensing.upgrade_url(),
        })
    ws = req.workspace or _default_ws()
    return _run(service().consolidate, workspace=ws, dry_run=req.dry_run, infer=req.infer,
                structured=req.structured, supersede_sources=req.supersede_sources)


# ── analytics (Pro) ───────────────────────────────────────────────────────────
@router.get("/analytics/portfolio")
def analytics_portfolio():
    """Portfolio computation moved to the hosted analytics surface."""
    raise HTTPException(status_code=501, detail={
        "error": "Portfolio analytics are available in the Engraphis Cloud dashboard.",
        "managed_cloud": True,
    })


@router.get("/analytics")
def analytics(workspace: Optional[str] = None):
    """Submit a consented, bounded workspace snapshot to hosted analytics."""
    from engraphis.cloud_features import run_managed_job

    ws = workspace or _require_ws()
    envelope = _managed_call(run_managed_job, service(), ws, "analytics")
    return envelope.get("result", envelope)


@router.get("/analytics/export")
def analytics_export(workspace: Optional[str] = None):
    """Not implemented here; use the same analytics data as JSON instead."""
    raise HTTPException(status_code=501, detail={
        "error": "This build does not generate analytics report files. Use GET /analytics "
                 "for the same data as JSON.",
        "implemented": False,
        "alternative": "/analytics",
    })


@router.get("/ready")
def ready():
    """Readiness (vs. /health liveness): the service builds — initializing the embedder
    backend — and the DB answers a trivial SELECT. 503 until both hold. Public probe."""
    from engraphis import __version__
    checks = {"db": False, "embedder": False}
    try:
        s = service()
        s.store.conn.execute("SELECT 1").fetchone()
        checks["db"] = True
        checks["embedder"] = getattr(s.engine, "embedder", None) is not None
    except Exception:  # noqa: BLE001
        pass
    is_ready = all(checks.values())
    from fastapi.responses import JSONResponse
    return JSONResponse({"ready": is_ready, "checks": checks, "version": __version__},
                        status_code=200 if is_ready else 503)


# ── workspace export (local, free) ────────────────────────────────────────────
@router.get("/export")
def export(workspace: Optional[str] = None, signed: bool = False,
           canonical: bool = False):
    """Portable v2 workspace dump, including temporal graph/code evidence and receipts.

    This is the free local data-portability path. ``signed=true`` was never
    implemented, so it must not claim availability in Engraphis Cloud either.
    """
    if signed:
        raise HTTPException(status_code=501, detail={
            "error": "Signed compliance exports are not implemented. Omit signed=true for "
                     "the unsigned workspace export, which contains the same data.",
            "implemented": False,
            "alternative": "/export",
        })
    ws = workspace or _default_ws()
    return _run(
        service().export_workspace,
        workspace=ws,
        recovery=True,
        canonical=canonical,
    )


# ── automated maintenance (Pro) ───────────────────────────────────────────────
class _AutomationReq(BaseModel):
    enabled: Optional[bool] = None
    cadence_hours: Optional[int] = None
    consolidate: Optional[bool] = None
    min_cluster: Optional[int] = None
    archive_below: Optional[float] = None
    workspaces: Optional[list] = None
    dream: Optional[bool] = None
    dream_enabled: Optional[bool] = None
    dream_min_new: Optional[int] = None
    dream_idle_minutes: Optional[int] = None
    infer: Optional[bool] = None


@router.get("/automation")
def automation_get(workspace: Optional[str] = None):
    """Read or provision the cloud-authoritative managed-maintenance policy."""
    from engraphis.cloud_features import (
        CloudFeatureClient,
        automation_bootstrap_phase,
        build_managed_snapshot,
        save_automation_bootstrap_phase,
    )

    ws = _require_ws(workspace)
    workspace_id = service()._lookup_workspace(ws)
    if not workspace_id:
        raise HTTPException(status_code=404, detail={
            "error": "The selected workspace does not exist.",
            "managed_cloud": True,
        })
    cloud = _managed_call(CloudFeatureClient.from_environment, workspace_id)
    policy = _managed_call(cloud.get_policy, workspace_id)
    # Version zero is the Cloud's explicit "no policy has ever been saved" sentinel.  Start
    # new Pro/Team workspaces on the recommended maintenance cadence immediately: the account
    # connection already authorizes managed compute, and a customer should not need to discover
    # an extra enable switch.  A persisted disabled policy has version >= 1, so an intentional
    # pause is never overwritten.
    try:
        unconfigured = int(policy.get("version", -1)) == 0
    except (AttributeError, TypeError, ValueError, OverflowError):
        unconfigured = False
    if unconfigured:
        bootstrap_workspace_id = workspace_id
        default_policy = {
            "enabled": True,
            "cadence_minutes": 1440,
            "dream_enabled": True,
            "dream_min_new": 25,
            "dream_idle_minutes": 15,
            "infer": False,
        }
        # Snapshot upload and policy persistence are separate private-service calls. Record
        # the completed upload locally before saving the policy so a transient failure of
        # that second call resumes at the policy step instead of uploading memory again
        # and consuming another generation on every dashboard refresh.
        with _automation_bootstrap_lock(cloud.organization_id, bootstrap_workspace_id):
            phase = automation_bootstrap_phase(
                service(), cloud.organization_id, bootstrap_workspace_id
            )
            if phase == "policy_saved":
                # The initial GET observed version zero before the other tab completed its
                # bootstrap. The durable phase is authoritative: do not upload or resave.
                # ``version`` must not remain the Cloud's stale zero sentinel, or the follower
                # would render an enabled schedule as unconfigured until its next refresh.
                policy = {**default_policy, "version": 1}
            else:
                if phase != "snapshot_uploaded":
                    workspace_id, snapshot = _managed_call(build_managed_snapshot, service(), ws)
                    receipt = _managed_call(cloud.upload_snapshot, workspace_id, snapshot)
                    generation = (
                        receipt.get("generation", snapshot["generation"])
                        if isinstance(receipt, dict)
                        else snapshot["generation"]
                    )
                    save_automation_bootstrap_phase(
                        service(),
                        cloud.organization_id,
                        bootstrap_workspace_id,
                        "snapshot_uploaded",
                        generation=int(generation),
                    )
                saved = _managed_call(cloud.save_policy, workspace_id, default_policy)
                save_automation_bootstrap_phase(
                    service(), cloud.organization_id, bootstrap_workspace_id, "policy_saved"
                )
                policy = {**default_policy, **(saved if isinstance(saved, dict) else {})}
    recent = _managed_call(cloud.list_jobs, workspace_id, limit=10)
    recent_jobs = recent.get("jobs") if isinstance(recent, dict) else []
    if not isinstance(recent_jobs, list):
        recent_jobs = []
    last_job = recent_jobs[0] if recent_jobs and isinstance(recent_jobs[0], dict) else {}
    return {
        "enabled": bool(policy.get("enabled")),
        "cadence_hours": max(1, int(policy.get("cadence_minutes") or 1440) // 60),
        "consolidate": True,
        "min_cluster": 3,
        "archive_below": 0.05,
        "workspaces": [ws],
        "dream": bool(policy.get("dream_enabled", True)),
        "dream_min_new": int(policy.get("dream_min_new") or 25),
        "dream_idle_minutes": int(policy.get("dream_idle_minutes") or 15),
        "infer": bool(policy.get("infer")),
        "last_run": last_job.get("updated_at") or last_job.get("created_at"),
        "recent_jobs": recent_jobs,
        "next_run_at": policy.get("next_run_at"),
        "version": policy.get("version", 0),
    }


@router.post("/automation")
def automation_set(req: _AutomationReq, workspace: Optional[str] = None):
    """Persist scheduling only in the private cloud data store."""
    from engraphis.cloud_features import CloudFeatureClient, build_managed_snapshot

    ws = _require_ws(workspace)
    workspace_id = service()._lookup_workspace(ws)
    if not workspace_id:
        raise HTTPException(status_code=404, detail={
            "error": "The selected workspace does not exist.",
            "managed_cloud": True,
        })
    cloud = _managed_call(CloudFeatureClient.from_environment, workspace_id)
    current = _managed_call(cloud.get_policy, workspace_id)
    current_hours = max(1, int(current.get("cadence_minutes") or 1440) // 60)
    cadence_hours = req.cadence_hours if req.cadence_hours is not None else current_hours
    enabled = req.enabled if req.enabled is not None else bool(current.get("enabled"))
    policy = {
        "enabled": enabled,
        "cadence_minutes": max(15, int(cadence_hours) * 60),
        "dream_enabled": (
            req.dream_enabled
            if req.dream_enabled is not None
            else req.dream
            if req.dream is not None
            else bool(current.get("dream_enabled", True))
        ),
        "dream_min_new": req.dream_min_new if req.dream_min_new is not None
        else int(current.get("dream_min_new") or 25),
        "dream_idle_minutes": req.dream_idle_minutes if req.dream_idle_minutes is not None
        else int(current.get("dream_idle_minutes") or 15),
        "infer": req.infer if req.infer is not None else bool(current.get("infer")),
    }
    if enabled:
        # Upload only when managed processing is actually enabled. Merely viewing or
        # disabling a cloud policy must never transfer memory content.
        workspace_id, snapshot = _managed_call(build_managed_snapshot, service(), ws)
        _managed_call(cloud.upload_snapshot, workspace_id, snapshot)
    saved = _managed_call(cloud.save_policy, workspace_id, policy)
    return {
        **policy,
        **saved,
        "cadence_hours": policy["cadence_minutes"] // 60,
        "dream": policy["dream_enabled"],
        "consolidate": True,
    }


class _MaintenanceReq(BaseModel):
    dry_run: bool = True


@router.post("/maintenance/run")
def maintenance_run(req: _MaintenanceReq, workspace: Optional[str] = None):
    """Submit consolidation to managed compute; results remain generation-bound."""
    from engraphis.cloud_features import run_managed_job

    ws = _require_ws(workspace)
    envelope = _managed_call(run_managed_job, service(), ws, "consolidate")
    result = envelope.get("result", envelope)
    return {
        "dry_run": True,
        "cloud_managed": True,
        "requested_apply": not req.dry_run,
        "workspaces": [ws],
        "runs": [{"workspace": ws, "consolidate": result}],
    }


# ── knowledge graph (entities + relations, scoped to a workspace) ──────────────
@router.get("/graph")
def graph(workspace: Optional[str] = None, limit: int = 2000,
          layers: Optional[str] = None, include_code: bool = False,
          repo: Optional[str] = None, full: bool = False,
          connected_only: bool = False,
          as_of: Optional[float] = None,
          valid_at: Optional[float] = None,
          known_at: Optional[float] = None):
    """Entity-relation network for a workspace — vis-network-ready nodes/edges
    plus type counts, top-connected, and connectivity stats.

    Delegates to :meth:`MemoryService.graph` (engraphis/service.py), which is
    also what the Inspector UI's ``/api/graph`` calls — one implementation, so
    the two UIs render identical graphs and share the same workspace-binding
    isolation guard. Previously this read the DB file directly with its own
    sqlite connection, which bypassed that guard entirely; routing through the
    service closes that gap.
    """
    ws = workspace or _default_ws()
    selected = None if layers is None else [
        x.strip() for x in layers.split(",") if x.strip()
    ]
    return _run(
        service().graph, workspace=ws, limit=limit, layers=selected,
        include_code=include_code, repo=repo, backfill=False, full=full,
        connected_only=connected_only,
        as_of=as_of, valid_at=valid_at, known_at=known_at,
    )


def _graph_csv(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    items = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if len(items) > 64 or any(len(item) > 200 for item in items):
        raise HTTPException(
            status_code=422,
            detail={"error": "graph filters allow at most 64 values of 200 characters"},
        )
    return items


@router.get("/graph/scene")
def graph_scene(workspace: Optional[str] = None, level: str = "overview",
                center_id: Optional[str] = None, system_id: Optional[str] = None,
                seeds: Optional[str] = None, repo: Optional[str] = None,
                layers: Optional[str] = None, relations: Optional[str] = None,
                entity_types: Optional[str] = None,
                memory_types: Optional[str] = None,
                as_of: Optional[float] = None,
                valid_at: Optional[float] = None,
                known_at: Optional[float] = None,
                time_from: Optional[float] = None,
                time_to: Optional[float] = None,
                depth: int = Query(default=1, ge=0, le=2),
                min_support: int = Query(default=1, ge=0, le=1_000_000),
                min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
                include_code: bool = False, code_overlay: Optional[bool] = None,
                include_weak_co_occurs: Optional[bool] = None,
                include_weak_cooccurrence: Optional[bool] = None,
                node_limit: Optional[int] = Query(default=None, ge=1, le=300),
                edge_limit: Optional[int] = Query(default=None, ge=0, le=900)):
    """Complete or focused evidence-backed graph scene with deterministic identity."""
    ws = workspace or _require_ws()
    weak_cooccurrence = (
        include_weak_cooccurrence
        if include_weak_cooccurrence is not None else
        include_weak_co_occurs
        if include_weak_co_occurs is not None else
        level.strip().lower() == "complete"
    )
    code_enabled = include_code if code_overlay is None else code_overlay
    if level.strip().lower() == "complete" and (node_limit is not None or edge_limit is not None):
        # This is route-owned, parameter-only validation.  It keeps the precise dashboard
        # guidance without ever serializing a service exception.
        raise HTTPException(status_code=400, detail={
            "error": "complete scenes do not accept node_limit or edge_limit; "
                     "use graph filters instead of silently truncating the chart",
        })
    return _run(
        service().graph_scene, workspace=ws, level=level,
        center_id=center_id, system_id=system_id, seeds=_graph_csv(seeds),
        repo=repo, layers=_graph_csv(layers), relations=_graph_csv(relations),
        entity_types=_graph_csv(entity_types), memory_types=_graph_csv(memory_types),
        as_of=as_of, valid_at=valid_at, known_at=known_at,
        time_from=time_from, time_to=time_to, depth=depth,
        min_support=min_support, min_confidence=min_confidence,
        include_weak_cooccurrence=weak_cooccurrence,
        include_code=code_enabled, node_limit=node_limit, edge_limit=edge_limit,
    )


@router.get("/graph/suggest")
def graph_suggest(q: str = "", query: Optional[str] = None,
                  workspace: Optional[str] = None,
                  repo: Optional[str] = None,
                  memory_types: Optional[str] = None,
                  as_of: Optional[float] = None,
                  valid_at: Optional[float] = None,
                  known_at: Optional[float] = None,
                  time_from: Optional[float] = None,
                  time_to: Optional[float] = None,
                  include_weak_cooccurrence: bool = False,
                  limit: int = Query(default=8, ge=1, le=25)):
    ws = workspace or _require_ws()
    return _run(
        service().graph_suggest, query if query is not None else q,
        workspace=ws, repo=repo, memory_types=_graph_csv(memory_types),
        as_of=as_of, valid_at=valid_at, known_at=known_at,
        time_from=time_from, time_to=time_to,
        include_weak_cooccurrence=include_weak_cooccurrence, limit=limit,
    )


@router.get("/graph/entities/{canonical_id}")
def graph_entity(canonical_id: str, workspace: Optional[str] = None,
                 repo: Optional[str] = None,
                 memory_types: Optional[str] = None,
                 as_of: Optional[float] = None,
                 valid_at: Optional[float] = None,
                 known_at: Optional[float] = None,
                 time_from: Optional[float] = None,
                 time_to: Optional[float] = None,
                 include_weak_cooccurrence: bool = True):
    ws = workspace or _require_ws()
    return _run(
        service().graph_entity, canonical_id, workspace=ws, repo=repo,
        memory_types=_graph_csv(memory_types), as_of=as_of,
        valid_at=valid_at, known_at=known_at,
        time_from=time_from, time_to=time_to,
        include_weak_cooccurrence=include_weak_cooccurrence,
    )


@router.get("/graph/entities/{canonical_id}/memories")
def graph_entity_memories(canonical_id: str, workspace: Optional[str] = None,
                          as_of: Optional[float] = None,
                          valid_at: Optional[float] = None,
                          known_at: Optional[float] = None):
    """Bounded evidence cards for one graph node, without rebuilding the full inspector."""
    ws = workspace or _require_ws()
    return _run(
        service().graph_entity_evidence, canonical_id, workspace=ws,
        as_of=as_of, valid_at=valid_at, known_at=known_at,
    )


@router.get("/graph/path")
def graph_path(source: str, target: str, workspace: Optional[str] = None,
               repo: Optional[str] = None, as_of: Optional[float] = None,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None,
               memory_types: Optional[str] = None,
               time_from: Optional[float] = None,
               time_to: Optional[float] = None,
               max_hops: int = Query(default=8, ge=1, le=8),
               max_visits: int = Query(default=10_000, ge=1, le=50_000),
               include_weak_cooccurrence: bool = False):
    ws = workspace or _require_ws()
    return _run(
        service().graph_path, source, target, workspace=ws, repo=repo,
        as_of=as_of, valid_at=valid_at, known_at=known_at,
        memory_types=_graph_csv(memory_types),
        time_from=time_from, time_to=time_to,
        max_hops=max_hops, max_visits=max_visits,
        include_weak_cooccurrence=include_weak_cooccurrence,
    )


class _GraphIndexReq(BaseModel):
    workspace: str
    repo: Optional[str] = None
    dry_run: bool = True
    extractor: str = Field(default="regex", pattern=r"^regex$")


class _GraphIndexCancelReq(BaseModel):
    workspace: str


@router.get("/graph/index/status")
def graph_index_status(workspace: Optional[str] = None):
    """Current generation and latest explicit graph-index job for a workspace."""
    return _run(service().graph_index_status, workspace=workspace or _require_ws())


@router.post("/graph/index/jobs")
def graph_index_start(req: _GraphIndexReq):
    """Start an idempotent, persisted graph-index job (dry-run by default)."""
    return _run(
        service().start_graph_index_job,
        workspace=req.workspace,
        repo=req.repo,
        dry_run=req.dry_run,
        extractor=req.extractor,
    )


@router.get("/graph/index/jobs/{job_id}")
def graph_index_job(job_id: str, workspace: Optional[str] = None):
    return _run(
        service().graph_index_job, job_id, workspace=workspace or _require_ws()
    )


@router.post("/graph/index/jobs/{job_id}/cancel")
def graph_index_cancel(job_id: str, req: _GraphIndexCancelReq):
    return _run(
        service().cancel_graph_index_job, job_id, workspace=req.workspace
    )


class _CodeIndexReq(BaseModel):
    workspace: str
    repo: str
    root_path: str
    languages: Optional[list] = None


def _http_code_index_path(root_path: str) -> str:
    """Resolve an HTTP indexing target beneath the single operator-owned root.

    The HTTP API is a remote trust boundary, unlike direct local MCP and CLI use.
    Keep its root independent from the broader engine allow-list and pass only the
    checked, canonical path to the service layer.
    """
    configured_root = os.environ.get("ENGRAPHIS_HTTP_INDEX_ROOT", "").strip()
    if not configured_root:
        configured_roots = os.environ.get("ENGRAPHIS_INDEX_ROOTS", "").split(os.pathsep)
        configured_root = next((value.strip() for value in configured_roots if value.strip()), "")
    if configured_root and not os.path.isabs(configured_root):
        raise _HttpCodeIndexConfigurationError("HTTP code index root must be configured absolutely")

    base = os.path.normcase(os.path.realpath(configured_root or os.getcwd()))
    candidate = os.path.normcase(os.path.realpath(os.path.join(base, root_path)))

    # Keep a separator on both values so /operator/root-copy cannot pass as a
    # child of /operator/root. The root itself remains an allowed target.
    base_prefix = base.rstrip(os.sep) + os.sep
    candidate_with_sep = candidate.rstrip(os.sep) + os.sep
    if not candidate_with_sep.startswith(base_prefix):
        raise ValidationError("code index root is outside the HTTP index root")
    return candidate_with_sep


@router.post("/code/index")
def code_index(req: _CodeIndexReq):
    try:
        root_path = _http_code_index_path(req.root_path)
    except _HttpCodeIndexConfigurationError:
        raise _http_index_configuration_error() from None
    except ValidationError:
        raise _invalid_request() from None
    return _run(
        service().index_repo, workspace=req.workspace, repo=req.repo,
        root_path=root_path, languages=req.languages,
    )


@router.get("/code/search")
def code_search(query: str, workspace: str, repo: str, limit: int = 20,
                as_of: Optional[float] = None,
                valid_at: Optional[float] = None,
                known_at: Optional[float] = None):
    return _run(
        service().search_code, query, workspace=workspace, repo=repo, limit=limit,
        as_of=as_of, valid_at=valid_at, known_at=known_at,
    )


class _CodePathReq(BaseModel):
    workspace: str
    repo: str
    source: str
    target: str
    max_depth: int = 8
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None


@router.post("/code/path")
def code_path(req: _CodePathReq):
    return _run(
        service().code_path, req.source, req.target, workspace=req.workspace,
        repo=req.repo, max_depth=req.max_depth, as_of=req.as_of,
        valid_at=req.valid_at, known_at=req.known_at,
    )


class _CodeImpactReq(BaseModel):
    workspace: str
    repo: str
    changed_files: list[str]
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None


@router.post("/code/impact")
def code_impact(req: _CodeImpactReq):
    return _run(
        service().code_impact, req.changed_files,
        workspace=req.workspace, repo=req.repo, as_of=req.as_of,
        valid_at=req.valid_at, known_at=req.known_at,
    )


@router.get("/code/export")
def code_export(workspace: str, repo: str,
                as_of: Optional[float] = None,
                valid_at: Optional[float] = None,
                known_at: Optional[float] = None):
    return _run(
        service().export_code_graph, workspace=workspace, repo=repo,
        as_of=as_of, valid_at=valid_at, known_at=known_at,
    )


# ── license ───────────────────────────────────────────────────────────────────
# Mirror of the private control plane's plan→feature table (read-only reference:
# the hosted entitlement contract the hosted plan-feature contract). Plans are lowercase
# and only ``pro``/``team`` are paid; any other value — unknown, empty, or mis-cased —
# resolves to no features, exactly as the server treats it.
#
# The server's own keys are {analytics, automation, export, sync, team}. This client's
# commercial manifest additionally names Auto Consolidation and Auto Dreaming, which the
# server grants under ``automation``. They are expanded here so the dashboard can never
# draw a lock on a capability the customer's plan already includes.
_AUTOMATION_FEATURES = ("automation", "consolidation", "dreaming")
_PRO_FEATURES = ("analytics", "sync") + _AUTOMATION_FEATURES
_HOSTED_ENTITLEMENTS = {
    "free": (),
    "local": (),
    "pro": _PRO_FEATURES,
    "team": _PRO_FEATURES + ("team",),
}

# Labels for the dashboard's entitlement list; it renders one row per key and ticks the
# ones ``features`` contains. Naming follows the customer-facing vocabulary in
# .env.example and the commercial manifest.
_FEATURE_LABELS = {
    "analytics": "Analytics",
    "automation": "Automation",
    "consolidation": "Auto Consolidation",
    "dreaming": "Auto Dreaming",
    "sync": "Cloud Sync",
    "team": "Team administration",
}


def entitled_features(plan: str) -> list:
    """Return the feature keys a hosted plan grants.

    Presentation only. This decides which lock badges the dashboard draws, never whether
    an operation is permitted: every paid operation is still authorized by Engraphis
    Cloud and gated on its response status.
    """

    return sorted(_HOSTED_ENTITLEMENTS.get(str(plan or "").strip().lower(), ()))


# ── authoritative plan resolution ─────────────────────────────────────────────────────
# The control plane knows the plan; this client has to be told. Inferring "connected ⇒ pro"
# showed a paying TEAM customer a PRO badge and a lock on the Team administration they were
# paying for, so the plan is now read rather than guessed.
#
# the hosted registration response — the body both the hosted registration endpoint and
# ``POST /v1/tokens/refresh`` answer with — carries ``plan``, ``cloud_features`` and
# ``cloud_access_active``. Those are the calls this client already makes, so the plan
# arrives with credentials it was going to mint anyway: ``cloud_session`` persists the
# fields on registration and re-confirms them on every token rotation, and
# ``_session_entitlement`` reads them back. No extra request, no separate cache, correct on
# the first boot after onboarding, and correct offline because the session record outlives
# the connection.
#
# Those fields are OPTIONAL here. A control plane that has not deployed them yet simply
# omits them, and the client falls back to ``GET /v1/entitlements/{organization_id}``,
# which returns the same ``plan`` and ``cloud_features``. Every access token this client
# can mint already carries the ``entitlement:read`` scope that route requires, and the
# route is declared ``require_workspace_binding=False``, so no workspace context is needed.
# That fallback answer is cached in its own file and is *strictly* outranked by the session
# record above, so the two persisted answers can never disagree silently.
#
# Neither answer is ever fetched inline. ``/api/license`` is on the ``/api/bootstrap`` boot
# path, and boot must not make a blocking, credential-bearing network call: a read answers
# from persisted state immediately and, when that state is missing or stale, refreshes it on
# a daemon thread so the next read is right. That background refresh is also what corrects a
# plan the customer changed in the account portal — a Pro→Team upgrade unlocks a tab the
# customer cannot click *until* it is unlocked, so nothing else would ever ask. Every
# failure — offline, lapsed, revoked, unreadable state directory, malformed body — degrades
# to the last known plan and finally to the inference below. Nothing here can raise into, or
# delay, ``/api/bootstrap``.

#: Cache envelope version; an unrecognised value is discarded rather than trusted.
_ENTITLEMENT_CACHE_SCHEMA = "engraphis-cloud-entitlement/v1"
#: How stale the cached entitlement may get before a read schedules a background refresh.
_ENTITLEMENT_REFRESH_SECONDS = 15 * 60
#: Bounded budget for the background fetch. It never runs on a request thread, but an
#: unbounded dial would still pin a daemon thread and a rotated credential indefinitely.
_ENTITLEMENT_TIMEOUT_SECONDS = 8.0
_ENTITLEMENT_MAX_RESPONSE_BYTES = 64 * 1024
_ENTITLEMENT_REFRESH_LOCK = threading.Lock()
_entitlement_refreshing = False
_ENTITLEMENT_RETRY_BASE_SECONDS = 30.0
_ENTITLEMENT_RETRY_MAX_SECONDS = 15 * 60.0
_entitlement_retry_after = 0.0
_entitlement_refresh_failures = 0
#: Same opt-out vocabulary as ``ENGRAPHIS_UPDATE_CHECK`` (see engraphis/update_check.py).
_FALSY_SETTINGS = {"0", "false", "no", "off", "disable", "disabled"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Block redirects on the credential-bearing entitlement read.

    The request carries a live bearer token, so a crafted 30x must not be able to replay
    it at another host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _entitlement_refresh_enabled() -> bool:
    """Background entitlement refresh is on by default; any falsy value disables it."""

    value = os.environ.get("ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH", "1").strip().lower()
    return value not in _FALSY_SETTINGS


def _entitlement_cache_path() -> Optional["Path"]:
    """Return the cache leaf beside the cloud session, or ``None`` if unlocatable."""

    try:
        root = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
        base = Path(root).expanduser() if root else Path.home() / ".engraphis"
    except (OSError, RuntimeError):  # no resolvable home directory
        return None
    return base / "cloud_entitlement.json"


def _cloud_control_url() -> str:
    """Return the configured control-plane base URL, or ``""``. Never raises.

    ``cloud_session`` exposes no public accessor for the saved endpoint, so the saved
    record is read through its loader defensively: a rename degrades to "not configured",
    which merely skips the refresh rather than breaking the boot path.
    """

    value = os.environ.get("ENGRAPHIS_CLOUD_CONTROL_URL", "").strip()
    if value:
        return value.rstrip("/")
    try:
        from engraphis import cloud_session
        loader = getattr(cloud_session, "_load", None)
        saved = loader() if loader is not None else None
    except Exception:  # noqa: BLE001 - an unreadable session is simply "not configured"
        return ""
    if not isinstance(saved, dict):
        return ""
    return str(saved.get("control_url") or "").strip().rstrip("/")


def _configured_organization_id() -> str:
    """Return the organization this installation is bound to *right now*, or ``""``.

    A pinned ``ENGRAPHIS_CLOUD_ORGANIZATION_ID`` wins, exactly as it does in
    ``cloud_session.access_for_workspace``; otherwise the saved session names it. Never
    raises: an unreadable or absent session simply means "unknown", and an unknown
    organization leaves the persisted answers in place rather than blanking a paying
    customer's badge.
    """

    pinned = os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
    if pinned:
        return pinned
    try:
        from engraphis import cloud_session
        loader = getattr(cloud_session, "_load", None)
        saved = loader() if loader is not None else None
    except Exception:  # noqa: BLE001 - an unreadable session is simply "not configured"
        return ""
    if not isinstance(saved, dict):
        return ""
    return str(saved.get("organization_id") or "").strip()


def _normalized_plan(value: object) -> str:
    """Map any control-plane plan name onto this client's presentation vocabulary."""

    plan = str(value or "").strip().lower()
    return plan if plan in ("pro", "team") else "local"


#: The control plane's entitlement status vocabulary, mirrored from (read-only)
#: the hosted entitlement contract the hosted status contract and the statuses
#: the hosted subscription endpoint accepts. Presentation only: an unrecognised value is
#: reported as ``""`` rather than guessed at, which degrades the copy to the generic
#: wording instead of asserting something the server never said.
_ENTITLEMENT_STATUSES = frozenset({
    "active", "trialing", "past_due", "canceled", "expired", "revoked", "scheduled",
    "inactive",
})


def _normalized_status(value: object) -> str:
    """Return the control plane's entitlement status, or ``""`` when it said nothing."""

    status = str(value or "").strip().lower()
    return status if status in _ENTITLEMENT_STATUSES else ""


def _epoch_seconds(value: object) -> float:
    """Return an ISO-8601 instant as epoch seconds, or ``0.0``. Never raises.

    The control plane serializes ``trial_ends_at`` as an ISO-8601 UTC timestamp; the
    dashboard renders instants as epoch seconds. Converting once, here, keeps the JS from
    parsing dates the CSP-externalized asset cannot be unit-tested through.

    ``datetime.fromisoformat`` on this package's Python 3.9 floor does not accept the
    trailing ``Z`` that Pydantic emits for UTC, so it is rewritten to the explicit offset
    first. Anything it still cannot parse is reported as "unknown" rather than raising on
    the ``/api/bootstrap`` boot path.

    Numeric input must additionally be *finite*. ``json.loads`` accepts the non-finite
    literals ``Infinity``/``NaN`` and turns an out-of-range value such as ``1e309`` into
    ``inf``, and either one would travel out through ``/api/license`` and ``/api/bootstrap``
    into Starlette's ``JSONResponse``, which raises ``ValueError`` on non-JSON-compliant
    floats — breaking the dashboard's boot path over a malformed cache entry. An
    unrepresentable instant is not a boundary; report it as unknown like any other
    unparseable value.
    """

    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):  # an arbitrary-precision JSON integer
            return 0.0
        return number if math.isfinite(number) and number > 0 else 0.0
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    try:
        return float(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return 0.0


def _trial_facts(source: object) -> dict:
    """Read the control plane's trial disclosure off any entitlement-shaped mapping.

    the hosted registration response and ``GET /v1/entitlements/{org}`` carry the same four
    fields, so one reader serves both and the two answers cannot be parsed by different
    rules. Every field is optional: a control plane that predates them simply omits them,
    and the honest answer is then "not a trial, none consumed" rather than a claim.

    ``trial_consumed`` is deliberately widened by ``is_trial``: an organization that is on
    a trial has by definition consumed one, so a server that answers only ``is_trial``
    still stops this client offering a second trial the server would refuse.
    """

    if not isinstance(source, dict):
        return {"status": "", "is_trial": False, "trial_consumed": False,
                "trial_ends_at": 0.0}
    is_trial = source.get("is_trial")
    is_trial = bool(is_trial) if isinstance(is_trial, bool) else False
    consumed = source.get("trial_consumed")
    consumed = bool(consumed) if isinstance(consumed, bool) else False
    return {
        "status": _normalized_status(source.get("status")),
        "is_trial": is_trial,
        "trial_consumed": consumed or is_trial,
        # A trial boundary is only meaningful for a trial. Replaying a converted
        # customer's long-past trial end would tell a paying subscriber their access
        # expired last month, which is exactly what the server refuses to do.
        "trial_ends_at": _epoch_seconds(source.get("trial_ends_at")) if is_trial else 0.0,
    }


def _unknown_trial_facts() -> dict:
    """Return the "the control plane has told us nothing" trial answer."""

    return {"status": "", "is_trial": False, "trial_consumed": False, "trial_ends_at": 0.0}


def _normalized_features(values: object, plan: str) -> list:
    """Keep the server's own grant, expanded to the names this dashboard renders.

    The server folds Auto Consolidation and Auto Dreaming into ``automation``; the license
    panel lists them separately, so echoing the server's keys verbatim would leave a Team
    customer looking at two unticked rows for capabilities they are paying for. Anything
    the dashboard cannot render is dropped, so ``features`` stays a subset of
    ``known_features`` even if a future server release adds a key this build predates.
    """

    allowed = set(entitled_features(plan))
    if not isinstance(values, (list, tuple)):
        return entitled_features(plan)
    granted = {str(item).strip().lower() for item in values if isinstance(item, str)}
    if "automation" in granted:
        granted.update(_AUTOMATION_FEATURES)
    # The payload is authoritative for a *subset* of the customer's plan grants, but it
    # must never escalate them.  In particular a stale or malformed Pro response that
    # still lists ``team`` used to unlock the Team UI even though the plan had already
    # changed.  The server still authorizes every operation, but presentation must be
    # conservative too.  Unknown future keys remain hidden as before.
    return sorted(granted & allowed & set(_FEATURE_LABELS))


def _session_entitlement() -> dict:
    """Return the entitlement the control plane put on this client's own session.

    Shaped exactly like ``_read_entitlement_cache`` so both persisted answers feed the
    resolver identically and only their precedence differs. Reads state only — no network —
    and never raises: this is on the ``/api/bootstrap`` boot path.
    """

    try:
        from engraphis import cloud_session
        reader = getattr(cloud_session, "saved_entitlement", None)
        declared = reader() if reader is not None else None
        if not isinstance(declared, dict) or not declared:
            return {}
        # A deployment pinned to ``ENGRAPHIS_CLOUD_ORGANIZATION_ID`` may be pointed at a
        # different organization than the saved session was registered for. Refuse to
        # relabel one customer's plan with another's, exactly as the entitlements read
        # refuses a mis-routed answer.
        pinned = os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
        if pinned and pinned != str(declared.get("organization_id") or ""):
            return {}
        plan = _normalized_plan(declared.get("plan"))
        active = bool(declared.get("cloud_access_active"))
        resolved = {
            "plan": plan,
            # The server empties ``cloud_features`` the moment paid access stops being
            # live; mirror that, exactly as the entitlements route's answer does below.
            # An older field-less body leaves ``cloud_features`` absent, and this client's
            # own plan table fills it in.
            "features": _normalized_features(declared.get("cloud_features"), plan)
            if active else [],
            "cloud_access_active": active,
            "organization_id": str(declared.get("organization_id") or ""),
            "fetched_at": float(declared.get("entitlement_checked_at") or 0.0),
        }
        resolved.update(_trial_facts(declared))
        return resolved
    except Exception:  # noqa: BLE001 - a badge must never break /bootstrap
        return {}


def _read_entitlement_cache() -> dict:
    """Return the last cached ``GET /v1/entitlements`` answer, or ``{}``. Never raises.

    Secondary to ``_session_entitlement``: this file exists only for a control plane that
    does not yet return the entitlement on registration and refresh.
    """

    path = _entitlement_cache_path()
    if path is None:
        return {}
    try:
        from engraphis.private_state import read_private_text
        raw = read_private_text(
            path, max_bytes=_ENTITLEMENT_MAX_RESPONSE_BYTES, allow_missing=True
        )
    except Exception:  # noqa: BLE001 - an unreadable cache is just "nothing known yet"
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != _ENTITLEMENT_CACHE_SCHEMA:
        return {}
    # Validate rather than coerce the plan: a corrupt value must be *discarded* so the
    # caller falls through to its own inference. Coercing it would quietly downgrade a
    # connected paying customer to the free local core on a damaged file.
    stored_plan = value.get("plan")
    if not isinstance(stored_plan, str) or stored_plan.strip().lower() not in (
        "pro", "team", "local", "free"
    ):
        return {}
    try:
        fetched_at = float(value.get("fetched_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        fetched_at = 0.0
    # Whose plan is this? The cache lives in the state directory, which outlives a
    # reconnect and is shared by every organization this installation is ever pointed at.
    # Serving it unchecked kept the PREVIOUS organization's Pro/Team badge and feature
    # grants on screen for a whole refresh interval — and indefinitely with
    # ``ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH=0``. Fail closed and ignore a cache that names
    # anyone else (or names nobody), exactly as ``_session_entitlement`` refuses a session
    # plan recorded for a different organization. The refresh scheduled by the caller then
    # writes the right one.
    # An *unknown* current organization is not permission to trust an organization-scoped
    # cache either. A fresh bootstrap has only a refresh credential and a control URL until
    # the first refresh names the organization, so a reused state directory would otherwise
    # serve the previous organization's paid plan for that whole window -- indefinitely with
    # ``ENGRAPHIS_CLOUD_ENTITLEMENT_REFRESH=0``. Require a match, not merely the absence of
    # a mismatch; the refresh the caller schedules writes the right answer.
    organization_id = str(value.get("organization_id") or "")
    current = _configured_organization_id()
    if not current or organization_id != current:
        return {}
    plan = _normalized_plan(stored_plan)
    # Mirror _session_entitlement: an inactive entitlement publishes no features. Today
    # hosted_plan_summary re-zeroes them downstream, but any future consumer reading
    # _plan_entitlement() directly would otherwise render paid rows on a lapsed account.
    active = bool(value.get("cloud_access_active"))
    resolved = {
        "plan": plan,
        "features": _normalized_features(value.get("features"), plan) if active else [],
        "cloud_access_active": active,
        "organization_id": organization_id,
        "fetched_at": fetched_at,
    }
    # The trial disclosure is cached under the same wire names the entitlements route uses,
    # so a cache written by an older build simply has none of them and reads back as "not a
    # trial" rather than as a corrupt entry.
    resolved.update(_trial_facts(value))
    return resolved


def _write_entitlement_cache(entitlement: dict) -> None:
    """Persist the authoritative entitlement so the next boot starts correct.

    Written through the owner-only atomic helper used for the cloud session itself: the
    plan is not a secret, but it lives in the same private state directory and a partial
    file must never be readable. Never raises — a read-only state directory costs
    freshness across restarts, not the dashboard.
    """

    path = _entitlement_cache_path()
    if path is None:
        return
    try:
        from engraphis.private_state import atomic_private_text
        atomic_private_text(path, json.dumps({
            "schema": _ENTITLEMENT_CACHE_SCHEMA,
            "plan": entitlement["plan"],
            "features": list(entitlement["features"]),
            "cloud_access_active": bool(entitlement["cloud_access_active"]),
            "organization_id": str(entitlement.get("organization_id") or ""),
            "fetched_at": float(entitlement.get("fetched_at") or time.time()),
            # Persisted under the wire names ``_trial_facts`` reads, so the cache round
            # trips through exactly one parser. ``trial_ends_at`` is already epoch seconds
            # by this point, which that parser also accepts.
            "status": _normalized_status(entitlement.get("status")),
            "is_trial": bool(entitlement.get("is_trial")),
            "trial_consumed": bool(entitlement.get("trial_consumed")),
            "trial_ends_at": float(entitlement.get("trial_ends_at") or 0.0),
        }, sort_keys=True, separators=(",", ":")), harden_parent=True)
    except Exception:  # noqa: BLE001 - losing the cache write must not surface anywhere
        logger.debug("entitlement cache write skipped")


def _deny_entitlement_cache() -> None:
    """Clear this cache's grants after an authoritative billing denial.

    ``cloud_session.record_billing_denial`` settles the session record, but an older
    control plane omits the entitlement fields from it entirely — that session stays
    planless, ``saved_entitlement()`` keeps answering ``{}``, and ``_plan_entitlement``
    falls through to *this* cache. Left alone it went on advertising paid features while
    every cloud operation was denied, which is the same disagreement one layer down.

    The plan name is preserved so the UI can still say which plan lapsed; only the access
    flag and the grants are cleared. Never raises: this runs on the refresh thread.

    ``fetched_at`` advances on *every* denial, including the repeat denial that finds the
    cache already settled. That case used to return without writing anything, which left
    ``_plan_entitlement`` serving a stale ``fetched_at`` and
    ``_refresh_entitlement_in_background`` re-scheduling a token refresh on every request
    against an account the control plane had already answered 402 for. A denial is an
    authoritative read; it has to advance the clock exactly as
    ``cloud_session.record_billing_denial`` now does one layer up.
    """

    try:
        cached = _read_entitlement_cache()
        if not cached:
            return
        denied = dict(cached)
        denied["cloud_access_active"] = False
        denied["features"] = []
        # The last status the server named now contradicts this denial, so it stops being
        # renderable copy. The trial facts survive: a lapse does not un-consume a trial or
        # move its boundary, and they are what distinguishes "your free trial ended" from
        # "your subscription lapsed" in the panel this settles.
        denied["status"] = ""
        denied["fetched_at"] = time.time()
        _write_entitlement_cache(denied)
    except Exception:  # noqa: BLE001 - a denial we cannot persist is still a denial
        logger.debug("entitlement cache denial skipped")


def _record_authoritative_denial() -> None:
    """Settle both persisted entitlement sources after a 401/402/403 cloud answer."""

    try:
        from engraphis.cloud_session import record_billing_denial

        record_billing_denial()
    except Exception:  # noqa: BLE001 - a denial we cannot persist is still a denial
        pass
    # An older control plane or direct-token deployment may have no entitlement fields in
    # the session record, so the compatibility cache must settle independently.
    _deny_entitlement_cache()


def _fetch_authoritative_entitlement() -> Optional[dict]:
    """Re-read the plan from the control plane. Returns ``None`` when nothing was cached.

    Two steps, in order:

    1. mint a token. ``access_for_workspace`` performs the ordinary token refresh, and a
       control plane that returns the entitlement on the hosted registration response has
       *already answered* by the time it comes back — ``cloud_session`` persisted the plan
       as a side effect of the call. That is the whole refresh; there is nothing to cache
       separately, so this returns ``None`` and the session record stays the one source.
    2. only if that produced no plan — an older control plane — fall back to
       ``GET /v1/entitlements/{organization_id}`` and cache what it says.

    Runs only on the background refresh thread; never on a request thread. No workspace
    binding is requested, so the issued token carries only the organization-scoped read
    scopes the entitlements route needs.
    """

    control = _cloud_control_url()
    if not control:
        return None
    try:
        from engraphis.cloud_session import access_for_workspace
        from engraphis.hosted_client import (
            build_pinned_https_opener,
            validate_cloud_base_url,
        )
        # Vet the endpoint before minting a credential for it. ``access_for_workspace``
        # validates the control URL on the saved-session path, but short-circuits without
        # validating it when a pinned ``ENGRAPHIS_CLOUD_ACCESS_TOKEN`` is configured — and
        # the pinned opener only replaces urllib's *HTTPS* handler, so an ``http://``
        # value would put a live bearer token on the wire in cleartext to an unvetted,
        # possibly private-range host. Validation also rejects embedded credentials and
        # re-resolves the host, closing the same DNS-rebinding window every other
        # outbound client in this package closes.
        control = validate_cloud_base_url(control)
        access_token, organization_id, _ = access_for_workspace(
            None, require_compute=False
        )
    except Exception as exc:  # noqa: BLE001 - offline, lapsed, revoked, invalid
        # A 402 is the control plane's authoritative answer that billing lapsed -- not a
        # transport hiccup. The saved session outranks this cache, so treating it like any
        # other failure left ``cloud_access_active`` true and kept paid features on the
        # dashboard indefinitely while every hosted call was denied. Persist the denial so
        # the two license surfaces cannot disagree; everything else is still "not now".
        # 401/403 are the control plane's "this session was revoked or deauthorized"
        # (cloud_session._refresh_http_error says exactly that). Falling through left
        # ``cloud_access_active`` true and every paid feature ticked on the dashboard
        # forever, which is the same defect 402 was fixed for.
        if getattr(exc, "status", None) in (401, 402, 403):
            _record_authoritative_denial()
        return None
    if not access_token or not organization_id:
        return None
    # Step 1 landed: the refresh above persisted plan/cloud_features/cloud_access_active,
    # which outranks this cache anyway. A second round trip would buy nothing.
    if _session_entitlement():
        return None
    request = urllib.request.Request(
        control + "/v1/entitlements/" + quote(organization_id, safe=""),
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + access_token,
            "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
        },
        method="GET",
    )
    try:
        with build_pinned_https_opener(_NoRedirect()).open(
            request, timeout=_ENTITLEMENT_TIMEOUT_SECONDS
        ) as response:
            raw = response.read(_ENTITLEMENT_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        # Draining and closing the error body can itself time out or reset, and a sibling
        # ``except`` clause of this ``try`` does not cover an exception raised inside this
        # handler. Leaving it unguarded is how a flaky cloud becomes an unhandled
        # traceback on a background thread.
        try:
            exc.read(_ENTITLEMENT_MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except (OSError, ValueError):
                pass
        if status in (401, 402, 403):
            _record_authoritative_denial()
        return None
    except Exception:  # noqa: BLE001 - transport, TLS, and URL failures are all "not now"
        return None
    if len(raw) > _ENTITLEMENT_MAX_RESPONSE_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(body, dict):
        return None
    # Refuse an answer about a different organization than this session is bound to, so a
    # mis-routed or replayed response can never relabel the customer's plan.
    if str(body.get("organization_id") or "") != organization_id:
        return None
    # ``plan`` and ``cloud_access_active`` are required fields of the server's
    # the hosted entitlement response. Demand both rather than defaulting them: a truncated or partial
    # body would otherwise coerce to "free, no access" and cache a paying customer as the
    # free local core. Absent means "no answer", which keeps the previous one.
    declared_plan = body.get("plan")
    active = body.get("cloud_access_active")
    if not isinstance(declared_plan, str) or not declared_plan.strip():
        return None
    if not isinstance(active, bool):
        return None
    plan = _normalized_plan(declared_plan)
    resolved = {
        "plan": plan,
        # The server empties ``cloud_features`` the moment paid access stops being live.
        # Mirroring that re-draws the locks on a lapsed subscription while the badge keeps
        # naming the plan the customer is on, so the dashboard offers the account portal
        # rather than a trial they have already used.
        "features": _normalized_features(body.get("cloud_features"), plan) if active else [],
        "cloud_access_active": active,
        "organization_id": organization_id,
        "fetched_at": time.time(),
    }
    # ``is_trial``/``trial_consumed``/``trial_ends_at``/``status`` are optional here for the
    # same reason the fields above the fallback are: a control plane that predates them
    # omits them, and the honest answer is then "not a trial", not a refusal to cache.
    resolved.update(_trial_facts(body))
    return resolved


def _refresh_entitlement_in_background(known: dict) -> None:
    """Re-confirm the plan off the request thread. Never blocks, never raises.

    Secondary by design. The plan normally arrives on registration and on every token
    refresh the client already makes, so this exists for the one case nothing else covers:
    a plan changed in the account portal, on an installation whose newly bought tab is
    locked and therefore cannot be clicked to trigger a refresh of its own.

    ``known`` is the answer currently being served (session record, else cached
    entitlement); its age decides whether to re-confirm. At most one refresh is in flight
    per process, and no thread is started at all unless a control-plane endpoint is actually
    configured, so an unconnected process does no background work whatsoever. Failed
    refreshes use bounded exponential backoff, preventing a dashboard read loop from
    amplifying a control-plane outage or repeatedly presenting one rotating credential.
    """

    global _entitlement_refreshing
    if not _entitlement_refresh_enabled():
        return
    age = time.time() - float(known.get("fetched_at") or 0.0)
    # A negative age means the clock moved backwards; re-check rather than trust it.
    if known and 0.0 <= age < _ENTITLEMENT_REFRESH_SECONDS:
        return
    if not _cloud_control_url():
        return
    with _ENTITLEMENT_REFRESH_LOCK:
        if _entitlement_refreshing or time.monotonic() < _entitlement_retry_after:
            return
        _entitlement_refreshing = True

    def _run() -> None:
        global _entitlement_refreshing
        global _entitlement_refresh_failures
        global _entitlement_retry_after
        refreshed = False
        before = float(known.get("fetched_at") or 0.0)
        try:
            fetched = _fetch_authoritative_entitlement()
            if fetched is not None:
                _write_entitlement_cache(fetched)
                refreshed = True
            else:
                current = _session_entitlement() or _read_entitlement_cache()
                refreshed = float(current.get("fetched_at") or 0.0) > before
        except Exception:  # noqa: BLE001 - a daemon thread must never surface anything
            pass
        finally:
            with _ENTITLEMENT_REFRESH_LOCK:
                if refreshed:
                    _entitlement_refresh_failures = 0
                    _entitlement_retry_after = 0.0
                else:
                    _entitlement_refresh_failures += 1
                    delay = min(
                        _ENTITLEMENT_RETRY_BASE_SECONDS
                        * (2 ** min(_entitlement_refresh_failures - 1, 10)),
                        _ENTITLEMENT_RETRY_MAX_SECONDS,
                    )
                    _entitlement_retry_after = time.monotonic() + delay
                _entitlement_refreshing = False

    try:
        threading.Thread(
            target=_run, name="engraphis-entitlement", daemon=True
        ).start()
    except Exception:  # noqa: BLE001 - a thread-starved host keeps the cached answer
        with _ENTITLEMENT_REFRESH_LOCK:
            _entitlement_refreshing = False


def _plan_entitlement() -> dict:
    """Resolve the customer's plan without ever blocking on the network.

    Order of precedence. One source wins outright at each step, so the persisted answers
    can never disagree silently:

    1. ``ENGRAPHIS_CLOUD_PLAN`` — an explicit operator override, kept as an escape hatch
       for air-gapped and pinned-token deployments;
    2. the entitlement the control plane returned on this installation's own registration
       or token refresh (``hosted registration response.plan`` / ``.cloud_features`` /
       ``.cloud_access_active``), persisted by ``cloud_session``. Primary: it rides calls
       the client already makes, costs no extra request, is re-confirmed on every token
       rotation, and outlives the connection so it is still right offline;
    3. the cached ``GET /v1/entitlements/{org}`` answer — the compatibility path for a
       control plane that does not return (2) yet, warmed on a background thread;
    4. inference — ``pro`` for a connected installation neither (2) nor (3) has answered
       for, the smallest paid plan, so a paying customer is never shown the free local
       core; ``local`` for an unconnected installation.

    One deployment cannot reach (2) or (3) at all: a pinned ``ENGRAPHIS_CLOUD_ACCESS_TOKEN``
    mints no refresh and so never re-confirms anything. That is precisely what (1) is
    documented for in ``.env.example``.

    Never raises: this feeds ``/api/license`` and therefore ``/api/bootstrap``.
    """

    declared = os.environ.get("ENGRAPHIS_CLOUD_PLAN", "").strip().lower()
    if declared in ("pro", "team", "free", "local"):
        plan = declared if declared in ("pro", "team") else "local"
        # An operator override names a plan, never a trial: it exists for air-gapped and
        # pinned-token deployments that have no control plane to ask. Reporting "no trial,
        # none consumed" is the honest answer, and it is what keeps the trial CTA off a
        # deployment that cannot start one.
        entitlement = {"plan": plan, "features": entitled_features(plan),
                       "source": "environment", "cloud_access_active": plan != "local",
                       "checked_at": 0.0}
        entitlement.update(_unknown_trial_facts())
        return entitlement
    try:
        from engraphis import cloud_session
        connected = cloud_session.configured(require_compute=False)
    except Exception:  # noqa: BLE001 - a badge must never break /bootstrap
        connected = False
    if not connected:
        entitlement = {"plan": "local", "features": [], "source": "local",
                       "cloud_access_active": False, "checked_at": 0.0}
        entitlement.update(_unknown_trial_facts())
        return entitlement
    session = _session_entitlement()
    if session:
        _refresh_entitlement_in_background(session)
        return _resolved_entitlement(session, source="session")
    cached = _read_entitlement_cache()
    _refresh_entitlement_in_background(cached)
    if cached:
        return _resolved_entitlement(cached, source="cloud")
    # Connected, but the control plane has never answered (first boot after onboarding, or
    # offline since). ``pro`` unlocks what every paid plan includes and leaves only the Team
    # upsell showing; the refresh scheduled above corrects it.
    #
    # It says nothing about a trial either way. Guessing "no trial consumed" here would
    # re-offer the trial CTA to a connected organization the server will refuse -- a
    # connected installation always has an organization, and that organization is on a
    # trial, has spent one, or is paying -- so this reports the trial as consumed and lets
    # the refresh replace the guess with the answer.
    entitlement = {"plan": "pro", "features": entitled_features("pro"),
                   "source": "connected", "cloud_access_active": True, "checked_at": 0.0}
    entitlement.update(_unknown_trial_facts())
    entitlement["trial_consumed"] = True
    return entitlement


def _resolved_entitlement(known: dict, *, source: str) -> dict:
    """Shape one persisted answer into the resolver's return value."""

    resolved = {
        "plan": known["plan"],
        "features": list(known["features"]),
        "source": source,
        "cloud_access_active": known["cloud_access_active"],
        "checked_at": known["fetched_at"],
    }
    resolved.update(_trial_facts(known))
    return resolved


def _hosted_plan() -> str:
    """Return the hosted plan this installation is on, else ``local``.

    The public client still holds no entitlement authority: this reports what the control
    plane last said (or, failing that, how the installation was provisioned). Forging any
    of its inputs grants nothing — the server authorizes every paid call.
    """

    return _plan_entitlement()["plan"]


#: The unpatched ``_hosted_plan``, captured so ``hosted_plan_summary`` can tell "nobody has
#: replaced the seam" from "somebody has". Calling ``_hosted_plan()`` unconditionally
#: resolved the entitlement a *second* time — two more state-file reads, and a second
#: chance to schedule a background refresh — on every ``/api/license`` and therefore every
#: ``/api/bootstrap`` request, for an answer already sitting in ``entitlement["plan"]``.
_DEFAULT_HOSTED_PLAN = _hosted_plan


def hosted_plan_summary() -> dict:
    """Return the one plan answer every license surface reports.

    ``/api/license`` and the legacy ``/memory/license`` both render this, so the two can
    never again disagree about what the customer has bought.
    """

    entitlement = _plan_entitlement()
    # ``_hosted_plan`` is the single override seam; when a caller replaces it, its answer
    # wins and the feature list falls back to this client's plan table. Left alone it
    # returns exactly ``_plan_entitlement()["plan"]``, so it is consulted only when it has
    # actually been replaced — calling it unconditionally repeated the resolution above
    # verbatim, state-file reads and refresh scheduling included.
    if _hosted_plan is not _DEFAULT_HOSTED_PLAN:
        plan = _hosted_plan()
        if plan != entitlement["plan"]:
            entitlement = {"plan": plan, "features": entitled_features(plan),
                           "source": "override", "cloud_access_active": plan != "local",
                           "checked_at": 0.0}
            entitlement.update(_unknown_trial_facts())
    state = _access_state(entitlement)
    # A grant this client cannot honour is not a grant. When the control plane itself
    # denies access it empties ``cloud_features`` and this is a no-op; when the *disclosed
    # trial boundary* is what ended the access, the last live answer's grants are still
    # sitting in the session record and the cache, and echoing them would tick feature rows
    # every hosted call is about to reject. One rule for both: the features are the ones
    # that are live right now.
    features = list(entitlement["features"]) if state in ("active", "trial") else []
    return {
        "plan": entitlement["plan"],
        "features": features,
        "plan_source": entitlement["source"],
        "cloud_access_active": entitlement["cloud_access_active"],
        "plan_checked_at": entitlement["checked_at"],
        "entitlement_status": entitlement["status"],
        "is_trial": entitlement["is_trial"],
        "trial_consumed": entitlement["trial_consumed"],
        "trial_ends_at": entitlement["trial_ends_at"],
        "access_state": state,
    }


#: Why the customer's hosted features are, or are not, available right now. The dashboard
#: renders one explanation per value; every one of them is a different thing to tell the
#: customer and a different thing to ask them to do.
#:
#: * ``trial`` — a live trial. Say when it ends and offer to buy.
#: * ``active`` — a live paid subscription. Nothing to explain.
#: * ``trial_expired`` — the free trial ran out. Offer to buy; never offer another trial.
#: * ``lapsed`` — a subscription that is no longer live (cancelled, expired, unpaid).
#:   Send the customer to billing, not to a trial they cannot start.
#: * ``inactive`` — no hosted plan at all. This is the only state a trial is offerable in.
#:
#: Before this existed, the last three were indistinguishable on screen: the client kept
#: ``plan="pro"`` with an emptied feature list, so a customer whose trial had ended and a
#: customer whose card had failed both saw a confident PRO badge over rows of locks with
#: no reason given.
_ACCESS_STATES = ("active", "trial", "trial_expired", "lapsed", "inactive")


def _trial_boundary_passed(ends_at: object, now: Optional[float] = None) -> bool:
    """Has the disclosed trial boundary already gone by? Unknown boundaries say no."""

    boundary = _epoch_seconds(ends_at)
    if boundary <= 0:
        return False
    return (time.time() if now is None else float(now)) >= boundary


def _access_state(entitlement: dict, now: Optional[float] = None) -> str:
    """Classify why hosted features are available or locked. Never raises."""

    if entitlement.get("plan") not in ("pro", "team"):
        return "inactive"
    is_trial = bool(entitlement.get("is_trial"))
    if not entitlement.get("cloud_access_active"):
        return "trial_expired" if is_trial else "lapsed"
    # ``cloud_access_active`` is only ever as fresh as the last answer from the control
    # plane, and a cached one can outlive the trial it describes: an installation that
    # stays offline past ``trial_ends_at`` keeps reading back the ``true`` saved while the
    # trial was live. Left alone this reported ``trial`` indefinitely — the dashboard
    # calling a trial live under a printed end date already in the past, and
    # ``/api/license`` advertising paid features every hosted call would be denied. The
    # boundary the server itself disclosed settles it without another request.
    if is_trial and _trial_boundary_passed(entitlement.get("trial_ends_at"), now):
        return "trial_expired"
    return "trial" if is_trial else "active"


@router.get("/license")
def get_license():
    """Hosted plan presentation for the dashboard; Cloud remains the authority.

    ``features`` drives the dashboard's lock badges. Hardcoding it empty drew a "PRO" or
    "TEAM" lock over Analytics, Automation, and Team for customers who had already paid
    for them, and inferring the plan drew a "PRO" badge plus a Team lock over a paying
    Team customer. Both now follow the control plane's own answer once it has been read.
    """
    summary = hosted_plan_summary()
    plan = summary["plan"]
    return {
        "plan": plan,
        "features": summary["features"],
        "known_features": dict(_FEATURE_LABELS),
        # Diagnostics for support: which resolution rule produced this plan — one of
        # ``environment`` (the operator override), ``session`` (the entitlement the cloud
        # returned on registration/refresh), ``cloud`` (the cached entitlements read),
        # ``connected`` or ``local`` (inference) — and when the control plane last
        # confirmed it (0 when it never has).
        "plan_source": summary["plan_source"],
        "plan_checked_at": summary["plan_checked_at"],
        "cloud_access_active": summary["cloud_access_active"],
        # Why the hosted features above are, or are not, live. The dashboard renders one
        # explanation per value rather than a plan badge over unexplained locks.
        "access_state": summary["access_state"],
        # The control plane's own entitlement status, when it named one. ``""`` means it
        # has not, or that its last answer was contradicted by a billing denial.
        "entitlement_status": summary["entitlement_status"],
        # The control plane owns the trial, and now says so on the calls this client
        # already makes. Hardcoding these to ``False`` made the dashboard's TRIAL badge
        # unreachable and, because ``used`` never became true, offered "Start your free
        # trial" forever — to active subscribers and to customers whose trial was already
        # spent, both of whom the server answers with ``TrialAlreadyConsumedError``.
        "is_trial": summary["is_trial"],
        "trial": {
            "used": summary["trial_consumed"],
            "active": summary["access_state"] == "trial",
            # Epoch seconds, ``0`` when there is no live trial boundary to disclose. A
            # converted paying customer is deliberately never told about a past one.
            "ends_at": summary["trial_ends_at"],
            # A trial is offerable only to an installation that belongs to no organization
            # yet. ``start_trial`` refuses every organization that already holds an
            # entitlement, so a connected customer — trialling, lapsed, or paying — can
            # only ever be answered 409 by the button.
            "available": (
                not summary["trial_consumed"]
                and summary["access_state"] == "inactive"
                and summary["plan_source"] == "local"
            ),
            "trial_days": licensing.TRIAL_DAYS,
        },
        "cloud_managed": True,
        "trial_seconds": licensing.TRIAL_SECONDS,
        "grace_seconds": licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS,
        "grace_scope": "private hosted account continuity only; free local core unaffected",
        "upgrade_url": licensing.upgrade_url(),
        # Pro and Team bill through separate checkout targets. Emitting only the generic
        # URL sent every Team upgrade click to the Pro page.
        "pro_upgrade_url": licensing.upgrade_url("pro", "monthly"),
        "team_upgrade_url": licensing.upgrade_url("team", "monthly"),
        "pro_monthly_upgrade_url": licensing.upgrade_url("pro", "monthly"),
        "pro_annual_upgrade_url": licensing.upgrade_url("pro", "annual"),
        "team_monthly_upgrade_url": licensing.upgrade_url("team", "monthly"),
        "team_annual_upgrade_url": licensing.upgrade_url("team", "annual"),
        # The plan-neutral account entry point, for the actions that are not a purchase —
        # "Open account portal" on a lapsed subscription above all. ``upgrade_url()``
        # cannot serve that: with no argument it resolves ``plan="pro"`` and prefers
        # ``ENGRAPHIS_PRO_UPGRADE_URL``, so wherever the checkout and the portal are
        # configured as distinct pages it is the Pro checkout wearing a neutral name.
        "account_url": licensing.account_url(),
    }


def _hosted_license_detail() -> dict:
    return {
        "error": "Start or manage Pro and Team in the Engraphis Cloud account portal.",
        "cloud_only": True,
        "trial_seconds": licensing.TRIAL_SECONDS,
        "grace_seconds": licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS,
        "grace_extends_cloud_access": False,
        "upgrade_url": licensing.upgrade_url(),
    }


@router.post("/license/activate")
@router.post("/license/trial")
@router.post("/license/team-trial")
@router.post("/license/trials")
def hosted_license_only():
    raise HTTPException(status_code=501, detail=_hosted_license_detail())


@router.get("/license/trials/{claim_id}")
def hosted_trial_status(claim_id: str):
    del claim_id
    raise HTTPException(status_code=501, detail=_hosted_license_detail())


@router.post("/ops/backup")
def run_customer_backup():
    """Commercial backup orchestration is hosted and no longer shipped here."""
    raise HTTPException(status_code=501, detail={
        "error": "Managed backups are operated by Engraphis Cloud.",
        "managed_cloud": True,
    })


@router.get("/ops/ready")
def customer_operations_ready():
    """The open local service has no commercial cloud-operations role."""
    return {"ready": True, "local_core": True, "managed_cloud": False}


# ── Cloud sync (Pro) — the dashboard's one-click "Sync now" button ────────────────────
# The heavy lifting is in core/sync.py + the RelayTransport client; these two routes just
# expose it to the dashboard so a user never touches a terminal. Sync is namespaced by
# workspace NAME (every device on the account shares a namespace); identity comes from a
# short-lived, workspace-scoped cloud bearer verified by the relay. See docs/SYNC.md.

#: Last-sync summary, per process, so the button can show "last synced …" without a store.
_SYNC_STATE: dict = {}


def _relay_url() -> str:
    # Falls back to the vendor default only if the operator blanked ENGRAPHIS_RELAY_URL;
    # DEFAULT_RELAY_URL lives in config so the literal is defined in exactly one place.
    return canonicalize_relay_url(settings.relay_url) or DEFAULT_RELAY_URL


@router.get("/sync/status")
def sync_status():
    """Whether one-click cloud sync is ready, plus the last-sync summary for the button."""
    from engraphis.backends.sync_relay import has_sync_token, sync_read_only
    from engraphis.cloud_session import CloudSessionError, configured

    has_token = has_sync_token()
    try:
        has_cloud_session = configured(require_compute=False)
    except CloudSessionError:
        has_cloud_session = False
    return {
        "available": bool(has_token or has_cloud_session),
        "has_key": False,
        "has_cloud_session": has_cloud_session,
        "has_user_token": has_token,
        "read_only": sync_read_only(),
        "token_managed_by_environment": bool(
            os.environ.get("ENGRAPHIS_SYNC_TOKEN", "").strip()),
        "read_only_managed_by_environment": bool(
            os.environ.get("ENGRAPHIS_SYNC_READ_ONLY", "").strip()),
        "plan": "cloud" if has_cloud_session else "local",
        "relay_url": _relay_url(),
        "tier_required": licensing.required_plan("sync"),
        "upgrade_url": licensing.upgrade_url(),
        "last": _SYNC_STATE.get("last"),
    }


def _sync_all(svc) -> dict:
    """Push every workspace's memories to the relay and pull every peer's — the shared
    core behind the dashboard's explicit 'Sync now' action.

    Never raises: a relay/transport failure on one workspace is captured in ``errors``
    (with the HTTP ``status`` when known) so a single bad workspace never aborts the rest,
    Returns the last-sync summary so the caller can surface a bounded error to a human."""
    from engraphis.backends.sync_folder import get_transport
    from engraphis.backends.sync_relay import RelayError, has_sync_token
    from engraphis.cloud_session import CloudSessionError, access_for_workspace
    from engraphis.core.sync import SyncEngine

    wss = svc.list_workspaces().get("workspaces") or []
    engine = svc.engine
    syncer = SyncEngine(engine.store, embedder=engine.embedder, vector_index=engine.index,
                        allowed_workspaces=settings.allowed_workspaces or None)
    totals = {"added": 0, "updated": 0, "unchanged": 0, "links_added": 0}
    # Distinct OTHER devices we pulled from, deduped across workspaces: the same peer
    # pushes a bundle per workspace, so summing per-workspace counts would multiply one
    # device by its workspace count. Counting unique device ids gives the true peer total.
    peer_devices: set = set()
    exported, errors = 0, []
    attempted, succeeded = 0, 0
    legacy_token_configured = has_sync_token()
    for w in wss:
        name = w.get("name")
        if not name:
            continue
        if w.get("visibility") == "personal":
            # Personal folders are private to their owner and must never leave this device
            # over the hosted organization relay: the relay namespace is shared by authorized
            # organization members, not partitioned per local user — pushing a personal
            # folder there would let any teammate pull it. Keep them local. (Both callers are
            # covered: the "Sync now" button runs in the owner-admin's request context, where
            # list_workspaces already hides *other* users' personal folders but still returns
            # the caller's own; the background loop runs with no user context and sees them
            # all. This skip is the single point that keeps either from syncing.)
            continue
        row = svc.store.conn.execute(
            "SELECT id, settings FROM workspaces WHERE name=?", (name,)).fetchone()
        if not row:
            continue
        # Fail CLOSED on unreadable settings, unlike the local-authorization
        # convention (which collapses malformed settings to "shared"): this path
        # uploads the folder off-device, so a corrupted settings row must block the
        # push rather than silently treat a possibly-personal folder as shared.
        try:
            raw_settings = json.loads(row["settings"] or "{}")
        except (TypeError, ValueError):
            raw_settings = None
        if not isinstance(raw_settings, dict):
            errors.append({
                "workspace": name,
                "error": "workspace settings are unreadable; refusing to sync to the "
                         "shared relay (the folder could be marked personal)",
            })
            continue
        visibility = raw_settings.get("visibility")
        if visibility == "personal":
            continue
        if visibility not in (None, "", "shared"):
            errors.append({
                "workspace": name,
                "error": "workspace visibility is invalid; refusing to sync to the "
                         "shared relay",
            })
            continue
        attempted += 1
        try:
            cloud_access = None
            if not legacy_token_configured:
                cloud_access, _, _ = access_for_workspace(name, require_compute=False)
            transport = get_transport(
                "relay",
                base_url=_relay_url(),
                workspace_id=name,
                access_token=cloud_access,
            )
            from engraphis.backends.sync_relay import sync_read_only
            read_only = sync_read_only()
            rep = syncer.sync(transport, row["id"], push=not read_only)
        except CloudSessionError as exc:
            status = exc.status if 400 <= exc.status <= 599 else 503
            if status in {401, 403}:
                message = "cloud session authorization failed"
            elif status == 409:
                message = "cloud session state is unavailable"
            else:
                message = "cloud session is temporarily unavailable"
            logger.warning(
                "cloud sync session failed (%s, status=%s)", type(exc).__name__, status
            )
            errors.append({"workspace": name, "error": message, "status": status})
            continue
        except RelayError as exc:
            # Record the HTTP status (402 == cloud authorization denied) instead of raising, so
            # one workspace can't abort the sweep; sync_run() promotes a 402 to the button.
            logger.warning("cloud sync relay failed (%s, status=%s)", type(exc).__name__,
                           exc.status)
            errors.append({"workspace": name, "error": "cloud relay synchronization failed",
                           "status": exc.status})
            continue
        except Exception as exc:  # noqa: BLE001 — one bad workspace must not abort the rest
            logger.error("sync workspace failed (%s)", type(exc).__name__)
            errors.append({"workspace": name, "error": "sync workspace failed"})
            continue
        succeeded += 1
        exported += int(rep.get("exported_memories", 0) or 0)
        for a in rep.get("applied") or []:
            dev = a.get("from_device")
            if dev and dev != "?" and "error" not in a:
                peer_devices.add(dev)
        for k in totals:
            totals[k] += int((rep.get("totals") or {}).get(k, 0) or 0)

    return {"at": time.time(), "workspaces": len(wss), "attempted": attempted,
            "succeeded": succeeded, "exported": exported,
            "peers": len(peer_devices), "added": totals["added"],
            "updated": totals["updated"], "unchanged": totals["unchanged"],
            "errors": errors}


@router.post("/sync/run")
async def sync_run():
    """Push this device's memories to the relay and pull every other device's — for every
    workspace. Backs the dashboard 'Sync now' button and requires a scoped cloud session."""
    from engraphis.backends.sync_relay import has_sync_token
    from engraphis.cloud_session import CloudSessionError, configured

    has_token = has_sync_token()
    try:
        has_cloud_session = configured(require_compute=False)
    except CloudSessionError as exc:
        # An unreadable cloud session must not strand installations that still authenticate
        # to the relay with a legacy sync token -- surface it only when there is no other
        # way in, otherwise fall through to the token path as before.
        if not has_token:
            status = exc.status if 400 <= exc.status <= 599 else 503
            raise HTTPException(status_code=status, detail={
                "error": "The saved cloud session is unavailable.",
                "upgrade_url": licensing.upgrade_url(),
            }) from None
        has_cloud_session = False
    if not has_token and not has_cloud_session:
        raise HTTPException(status_code=402, detail={
            "error": "Connect this installation to Engraphis Cloud before syncing.",
            "upgrade_url": licensing.upgrade_url()})

    svc = service()
    if not (svc.list_workspaces().get("workspaces") or []):
        raise HTTPException(status_code=400,
                            detail={"error": "Nothing to sync yet — add a memory first."})

    import asyncio
    summary = await asyncio.to_thread(_sync_all, svc)
    _SYNC_STATE["last"] = summary
    # Promote a total authorization loss to the dashboard's recovery CTA.  Successful
    # empty/read-only workspaces still count as successes, so exported == 0 is not enough:
    # every attempted shared workspace must have failed with an authorization status, and
    # no different workspace error may be hidden behind the recovery prompt.
    authorization_statuses = {401, 402, 403}
    authorization_errors = [
        error for error in summary["errors"]
        if error.get("status") in authorization_statuses
    ]
    if (
        summary["attempted"] > 0
        and summary["succeeded"] == 0
        and len(authorization_errors) == summary["attempted"]
        and len(authorization_errors) == len(summary["errors"])
    ):
        first = authorization_errors[0]
        raise HTTPException(status_code=first["status"], detail={
            "error": first["error"], "upgrade_url": licensing.upgrade_url()})
    return {"ok": True, "summary": summary}


class _SyncTokenReq(BaseModel):
    token: str = Field(..., min_length=24, max_length=8192)
    read_only: bool = False


@router.post("/sync/token")
def configure_sync_token(req: _SyncTokenReq):
    from engraphis.backends.sync_relay import (
        save_sync_read_only, save_sync_token, sync_read_only)
    env_token = os.environ.get("ENGRAPHIS_SYNC_TOKEN", "").strip()
    if env_token and not hmac.compare_digest(env_token, req.token.strip()):
        raise HTTPException(status_code=409, detail={
            "error": "sync token is managed by ENGRAPHIS_SYNC_TOKEN"})
    env_policy = os.environ.get("ENGRAPHIS_SYNC_READ_ONLY", "").strip()
    if env_policy and sync_read_only() != bool(req.read_only):
        raise HTTPException(status_code=409, detail={
            "error": "read-only policy is managed by ENGRAPHIS_SYNC_READ_ONLY"})
    try:
        with _sync_token_state_lock:
            # A partial update must fail toward no uploads. Persist a restrictive sentinel
            # before replacing the token; relax it only after token persistence succeeds.
            save_sync_read_only(True)
            if not env_token:
                save_sync_token(req.token, relay_origin=_relay_url())
            if not req.read_only:
                save_sync_read_only(False)
    except ValueError as exc:
        logger.info("sync token configuration rejected (%s)", type(exc).__name__)
        raise HTTPException(status_code=400,
                            detail={"error": "invalid sync token configuration"}) from None
    except OSError:
        raise HTTPException(status_code=503, detail={
            "error": "sync token state could not be persisted"})
    return {"configured": True, "read_only": bool(req.read_only),
            "token_managed_by_environment": bool(env_token),
            "read_only_managed_by_environment": bool(env_policy)}


@router.delete("/sync/token")
def remove_sync_token():
    from engraphis.backends.sync_relay import clear_sync_token, has_sync_token, sync_read_only
    try:
        with _sync_token_state_lock:
            clear_sync_token()
    except OSError:
        raise HTTPException(status_code=503, detail={
            "error": "sync token state could not be removed"})
    # An explicit deployment environment token cannot be removed by a dashboard file
    # operation. Report the effective state instead of claiming it disappeared.
    return {"configured": has_sync_token(), "read_only": sync_read_only()}
