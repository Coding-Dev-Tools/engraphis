"""v1-dashboard API adapter over the v2 MemoryService (engraphis/service.py).

Serves the restored v1 dashboard on the *v2* engine — same look, real (v2) data.
Every route is under /api and returns plain JSON the dashboard's JS consumes. Paid
surfaces (analytics, export) gate through engraphis.licensing. Team auth lives in
engraphis/routes/v2_team.py and is included by the dashboard app.
"""
from __future__ import annotations

import json
import hmac
import logging
import os
import threading
import time
import weakref
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.config import DEFAULT_RELAY_URL, canonicalize_relay_url, settings
from engraphis.netutil import is_local_request
from engraphis.service import (
    GraphIndexRebuilding,
    GraphSceneCapacityExceeded,
    MemoryService,
    ValidationError,
)
from engraphis.core.store import _escape_like

router = APIRouter(prefix="/api", tags=["dashboard"])
logger = logging.getLogger("engraphis.api")

_service: Optional[MemoryService] = None


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


def _paid(feature: str) -> None:
    try:
        licensing.require_feature(feature)
    except licensing.LicenseError as exc:
        raise HTTPException(status_code=402, detail={
            "error": str(exc), "feature": exc.feature or feature,
            "tier_required": licensing.required_plan(feature),
            "upgrade_url": licensing.upgrade_url()})


def _run(fn, *a, **k):
    """Call a service method, mapping validation errors to 400 and the rest to 500."""
    try:
        return fn(*a, **k)
    except GraphIndexRebuilding as exc:
        raise HTTPException(status_code=409, detail={
            "error": str(exc), "index_state": "rebuilding", "job_id": exc.job_id,
        })
    except GraphSceneCapacityExceeded as exc:
        raise HTTPException(status_code=413, detail={
            "error": str(exc),
            "safety_state": "capacity_exceeded",
            "degraded": True,
            "truncated": False,
            "resource": exc.resource,
            "count": exc.count,
            "limit": exc.limit,
            "recommended_action": "narrow repository, time, type, or relation filters",
        })
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "not aligned" in msg or ("256" in msg and "384" in msg):
            raise HTTPException(status_code=409, detail={
                "error": "Semantic search needs the embedding model that built your data "
                         "(sentence-transformers / all-MiniLM). Install it once — "
                         "pip install \"sentence-transformers>=2.7\" — then restart the "
                         "dashboard. The Memories, Graph, Overview and Audit tabs work without it.",
                "embedder": True})
        logger.error("dashboard operation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"error": "internal server error"})


def _default_ws() -> Optional[str]:
    wss = service().list_workspaces().get("workspaces") or []
    return wss[0]["name"] if wss else None


def _require_ws() -> str:
    """Like _default_ws but raises 400 if no workspace exists yet."""
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
        "retention": m.get("retention"),
        "pinned": bool(m.get("pinned", False)),
        "importance": m.get("importance"),
        "valid_from": m.get("valid_from"),
        "valid_to": m.get("valid_to"),
        "expired_at": m.get("expired_at"),
        "ingested_at": m.get("ingested_at"),
        "provenance": m.get("provenance") or {},
    }


def _is_embedder_mismatch(exc) -> bool:
    msg = str(exc)
    return "not aligned" in msg or ("256" in msg and "384" in msg)


def _keyword_search(ws, q, limit=20):
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
        sql = ("SELECT id, scope, mtype, title, content, summary, pinned, importance, "
               "valid_from, valid_to, provenance FROM memories WHERE workspace_id=? "
               "AND valid_to IS NULL AND expired_at IS NULL")
        args = [row["id"]]
        terms = [t for t in (q or "").split() if len(t) > 2][:6]
        if terms:
            sql += " AND (" + " OR ".join(["title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'" for _ in terms]) + ")"
            for t in terms:
                args += ["%" + _escape_like(t) + "%", "%" + _escape_like(t) + "%"]
        sql += " ORDER BY COALESCE(last_access, valid_from) DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    def _prov(pp):
        try:
            return _json.loads(pp) if isinstance(pp, str) and pp else {}
        except Exception:  # noqa: BLE001
            return {}
    return [{"id": r["id"], "document_id": r["id"], "title": r["title"] or "",
             "content": r["content"] or r["summary"] or "", "memory_type": r["mtype"] or "semantic",
             "scope": r["scope"] or "", "pinned": bool(r["pinned"]),
             "importance": r["importance"], "valid_from": r["valid_from"],
             "valid_to": r["valid_to"], "provenance": _prov(r["provenance"])} for r in rows]


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
    lic = licensing.current_license(refresh=True).to_public_dict()
    lic["error"] = licensing.license_error()
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
        from engraphis.backends import embedder_st as _est
        from engraphis.backends.embedder_deterministic import DeterministicEmbedder
        e = current_service.engine.embedder
        d = int(getattr(e, "dim", 0))
        semantic = not isinstance(e, DeterministicEmbedder)
        emb = {"class": type(e).__name__, "dim": d, "semantic": semantic,
               "model": settings.embed_model, "error": getattr(_est, "LAST_EMBEDDER_ERROR", "")}
    except Exception:  # noqa: BLE001
        pass
    return {
        "license": lic,
        "workspaces": wss,
        "stats": _run(current_service.stats, workspace=scoped_stats_workspace),
        "embedder": emb,
        "features": {"graph_ui_v2": bool(settings.graph_ui_v2)},
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
    with _llm_extractor_lock:
        _llm_connection_state.update({
            "ok": bool(result.get("ok")),
            "provider": str(result.get("provider") or settings.llm_provider),
            "model": str(result.get("model") or settings.llm_model),
            "tested_at": time.time(),
        })


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
    plus a ready-to-paste .env snippet for the dashboard's "Connect your LLM" card.
    Never returns the API key itself — only whether one is set."""
    provider = settings.llm_provider or "openai"
    model = settings.llm_model or _LLM_DEFAULT_MODELS.get(provider, "")
    key_set = bool(settings.llm_api_key)
    verified = bool(key_set and _llm_is_verified(provider, model))
    return {
        "provider": provider,
        "model": model,
        "key_set": key_set,
        "base_url": settings.llm_base_url or "",
        "extractor": settings.extractor,
        "extractor_enabled": _extractor_enabled(),
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
            + (f"ENGRAPHIS_LLM_BASE_URL={settings.llm_base_url}\n" if settings.llm_base_url else "")
            + ("ENGRAPHIS_EXTRACTOR=llm_structured\n" if key_set else "# set ENGRAPHIS_EXTRACTOR=llm_structured to use it\n")
            + "ENGRAPHIS_LLM_AUTO_EXTRACT=1\n"
        ),
    }


@router.post("/llm/test")
def llm_test():
    """Live-test the configured LLM with a tiny completion. POST so the dashboard auth
    gate (member+ in team mode) applies — testing spends a fraction of a cent of the
    instance's API credit, so it's not a viewer action. Returns the ping result; never
    raises (the client's ping() already swallows every failure into ``ok=False``)."""
    if not settings.llm_api_key:
        _record_llm_test({"ok": False})
        return {"ok": False, "error": "No API key configured. Set ENGRAPHIS_LLM_API_KEY in your .env and restart.",
                "provider": settings.llm_provider, "model": settings.llm_model}
    try:
        from engraphis.llm.client import LLMClient
        with LLMClient() as llm:
            result = llm.ping()
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
        return result
    except Exception as exc:  # noqa: BLE001
        _record_llm_test({"ok": False})
        logger.error("LLM connection test failed (%s)", type(exc).__name__)
        return {"ok": False, "error": "The provider test failed. Check the configured "
                                      "provider, model, and network connection.",
                "provider": settings.llm_provider, "model": settings.llm_model}


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
    _record_llm_test(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail={
            "error": result.get("error") or "The configured LLM is not working."})
    return {"ok": True, "provider": result.get("provider"),
            "model": result.get("model"), **_set_llm_extractor(True)}


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
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    row = service().store.conn.execute(
        "SELECT id FROM workspaces WHERE name=?", (ws,)
    ).fetchone()
    if row is None:
        return {"workspace": ws, "count": 0, "activities": []}
    rows = service().store.conn.execute(
        "SELECT id, title, content, mtype, ingested_at, metadata FROM memories "
        "WHERE workspace_id=? AND valid_to IS NULL AND expired_at IS NULL AND ("
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


@router.post("/memory/update")
def memory_update(req: _UpdateMemReq):
    ws = req.workspace or _default_ws()
    return _run(service().update_memory, req.id, workspace=ws,
                title=req.title, mtype=req.memory_type)


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
           mtype: Optional[str] = None):
    ws = workspace or _default_ws()
    mtypes = [mtype] if mtype else None
    try:
        out = service().recall(q, workspace=ws, k=k, mtypes=mtypes, reinforce=False)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        if not _is_embedder_mismatch(exc):
            logger.error("dashboard recall failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail={"error": "internal server error"})
        mems = _keyword_search(ws, q, k)
        return {"query": q, "workspace": ws, "count": len(mems), "context": "",
                "memories": mems, "mode": "keyword",
                "note": "Keyword match — install sentence-transformers for semantic search."}
    return {"query": q, "workspace": ws, "count": out.get("count", 0),
            "context": out.get("context", ""), "mode": "semantic",
            "memories": [_mem(m) for m in out.get("memories", [])]}


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
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    conn = _sql.connect("file:%s?mode=ro" % settings.db_path, uri=True)
    conn.row_factory = _sql.Row
    try:
        row = conn.execute("SELECT id FROM workspaces WHERE name=?", (ws,)).fetchone()
        if row is None:
            return {"workspace": ws, "count": 0, "memories": []}
        sql = ("SELECT id, scope, mtype, title, content, summary, importance, pinned, "
               "valid_from, valid_to, provenance FROM memories WHERE workspace_id=? "
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
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
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
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
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
# The remote agent write path. An agent authenticates with a per-user bearer token
# (POST /api/auth/token) and stores memories on THIS cloud instance's v2 store — the
# same DB the dashboard reads — instead of running Engraphis locally. Gated by the
# instance's Team license (``_paid('team')``) so a free / lapsed instance can't host
# team agents: 402 without it. Workspace scoping + personal-folder ownership come from
# the auth gate's ``set_current_user`` (see dashboard_app._auth_gate). Parameters mirror
# the local MCP ``engraphis_remember`` tool so an agent gets identical semantics whether
# it writes locally or to the cloud.
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


@router.post("/remember")
def remember(req: _RememberReq):
    _paid("team")
    return _run(service().remember, req.content, workspace=req.workspace,
                repo=req.repo, mtype=req.mtype, scope=req.scope, title=req.title,
                importance=req.importance, keywords=req.keywords, metadata=req.metadata,
                source=req.source, trusted=req.trusted, resolve_conflicts=req.dedupe,
                retention_class=req.retention_class,
                retention_reason=req.retention_reason)


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


@router.post("/intent/remember")
def intent_remember(req: _IntentRememberReq):
    # Team-gated exactly like /api/remember: this is the intent-native agent WRITE path
    # onto this cloud instance's store, so a free/lapsed instance must not host it (402).
    _paid("team")
    return _run(
        service().intent_remember, req.text, workspace=req.workspace, repo=req.repo,
        title=req.title, mtype=req.mtype, scope=req.scope, importance=req.importance,
        metadata=req.metadata, retention_class=req.retention_class,
        retention_reason=req.retention_reason,
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
    # Team-gated like /api/remember and /api/intent/remember — same cloud write surface.
    _paid("team")
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


@router.post("/intent/recall")
def intent_recall(req: _IntentRecallReq):
    return _run(
        service().intent_recall, req.query, intent=req.intent,
        workspace=req.workspace or _default_ws(), repo=req.repo,
        mtypes=req.mtypes, k=req.k, as_of=req.as_of,
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
        _paid("automation")
    ws = req.workspace or _default_ws()
    return _run(service().consolidate, workspace=ws, dry_run=req.dry_run, infer=req.infer,
                structured=req.structured, supersede_sources=req.supersede_sources)


# ── analytics (Pro) ───────────────────────────────────────────────────────────
@router.get("/analytics/portfolio")
def analytics_portfolio():
    """Cross-workspace rollup. Same gate as /analytics; the workspace set comes
    from list_workspaces(), so team-auth boundaries (allowed_workspaces) hold."""
    _paid("analytics")
    from engraphis.analytics import compute_portfolio
    svc = service()
    wss = _run(svc.list_workspaces).get("workspaces") or []
    pairs = [(wid, w["name"]) for w in wss
             if (wid := svc._lookup_workspace(w["name"]))]
    return _run(compute_portfolio, svc.store, pairs)


@router.get("/analytics")
def analytics(workspace: Optional[str] = None):
    """Rich per-workspace analytics (growth, retention histogram, decay forecast,
    resolver mix, top entities) — the full engine analytics the Inspector used to own,
    now served here. Falls back to the lightweight summary only when no workspace can be
    resolved (e.g. a brand-new store). Pro-gated inside ``compute_analytics`` too."""
    _paid("analytics")
    svc = service()
    ws = workspace or _default_ws()
    wid = svc._lookup_workspace(ws) if ws else None
    if not wid:
        return _analytics_summary(workspace)
    from engraphis.analytics import compute_analytics
    return _run(compute_analytics, svc.store, wid)


@router.get("/analytics/export")
def analytics_export(workspace: Optional[str] = None):
    """Self-contained HTML analytics report (inline CSS, zero CDN) — a shareable,
    archivable artifact. Same Pro gate as the analytics view it renders."""
    _paid("analytics")
    from engraphis import __version__
    from engraphis.analytics import compute_analytics, render_analytics_html
    from fastapi.responses import HTMLResponse
    svc = service()
    ws = workspace or _require_ws()
    wid = svc._lookup_workspace(ws)
    if not wid:
        raise HTTPException(status_code=400, detail={"error": "Unknown workspace '%s'." % ws})
    page = render_analytics_html(_run(compute_analytics, svc.store, wid),
                                 workspace=ws, version=__version__)
    fname = "engraphis-analytics-%s-%s.html" % (
        ws.replace("/", "_"), __import__("time").strftime("%Y%m%d"))
    return HTMLResponse(page, headers={
        "Content-Disposition": 'attachment; filename="%s"' % fname})


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


def _analytics_summary(workspace: Optional[str]) -> dict:
    """Lightweight analytics summary (by-type + per-namespace distribution). The
    Pro gate lives HERE, at the top of the computation, so the payload can never
    be assembled on the free tier even if the route's ``_paid`` wrapper is deleted
    (defense in depth; mirrors engraphis.analytics.compute_analytics)."""
    licensing.require_cloud_lease("analytics")
    st = _run(service().stats, workspace=workspace)
    wss = _run(service().list_workspaces).get("workspaces") or []
    by_type = [{"bucket": t, "count": c} for t, c in (st.get("by_type") or {}).items()]
    ws_dist = [{"namespace": w["name"], "count": w.get("memories", 0)} for w in wss]
    return {"by_type": by_type, "namespace_distribution": ws_dist,
            "total_memories": st.get("memories", 0), "sessions": st.get("sessions", 0),
            "workspaces": st.get("workspaces", 0)}


# ── compliance export (Pro) ───────────────────────────────────────────────────
def _sign_export(data: dict, workspace: str) -> dict:
    """Wrap a raw workspace dump in a tamper-evident compliance manifest.

    The manifest records the engine version, generation time, per-table record
    counts, the active license fingerprint, and a SHA-256 over the canonical JSON of
    the payload — so an archived export can be verified byte-for-byte years later
    without any Engraphis install. This is what turns a ``SELECT *`` dump into an
    audit-grade artifact."""
    import hashlib
    import json as _json
    import time as _time
    from engraphis import __version__
    canonical = _json.dumps(data, sort_keys=True, separators=(",", ":"),
                            default=str).encode("utf-8")
    lic = licensing.current_license()

    def _count(v):
        return len(v) if isinstance(v, (list, dict)) else None
    counts = {k: _count(v) for k, v in data.items() if _count(v) is not None}
    return {
        "manifest": {
            "format": "engraphis-compliance-export/v1",
            "engraphis_version": __version__,
            "workspace": workspace,
            "generated_at": int(_time.time()),
            "record_counts": counts,
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "licensed_to": lic.email or None,
            "license_plan": lic.plan,
            "license_key_id": lic.key_id or None,
        },
        "data": data,
    }


@router.get("/export")
def export(workspace: Optional[str] = None, signed: bool = False):
    """Full bi-temporal workspace dump (memories + sessions + audit). Pro-gated.

    ``signed=true`` wraps the dump in a SHA-256 compliance manifest (see
    :func:`_sign_export`) — a tamper-evident, self-verifying audit bundle."""
    _paid("export")
    ws = workspace or _default_ws()
    data = _run(service().export_workspace, workspace=ws)
    return _sign_export(data, ws or "") if signed else data


# ── automated maintenance (Pro) ───────────────────────────────────────────────
class _AutomationReq(BaseModel):
    enabled: Optional[bool] = None
    cadence_hours: Optional[int] = None
    consolidate: Optional[bool] = None
    min_cluster: Optional[int] = None
    archive_below: Optional[float] = None
    workspaces: Optional[list] = None
    dream: Optional[bool] = None
    dream_min_new: Optional[int] = None
    dream_idle_minutes: Optional[int] = None
    infer: Optional[bool] = None


@router.get("/automation")
def automation_get():
    """Current maintenance policy + last-run telemetry. Pro-gated (``automation``)."""
    _paid("automation")
    from engraphis import automation
    return automation.load_policy()


@router.post("/automation")
def automation_set(req: _AutomationReq):
    """Persist the maintenance policy. Pro-gated (``automation``)."""
    _paid("automation")
    from engraphis import automation
    current = automation.load_policy()
    merged = {k: (getattr(req, k) if getattr(req, k) is not None else current.get(k))
              for k in ("enabled", "cadence_hours", "consolidate", "min_cluster",
                        "archive_below", "workspaces",
                        "dream", "dream_min_new", "dream_idle_minutes", "infer")}
    return _run(automation.save_policy, merged)


class _MaintenanceReq(BaseModel):
    dry_run: bool = True


@router.post("/maintenance/run")
def maintenance_run(req: _MaintenanceReq):
    """Run the maintenance sweep now (dry-run by default). Pro-gated (``automation``)."""
    _paid("automation")
    from engraphis import automation
    return _run(automation.run_maintenance, service(), dry_run=req.dry_run)


# ── knowledge graph (entities + relations, scoped to a workspace) ──────────────
@router.get("/graph")
def graph(workspace: Optional[str] = None, limit: int = 2000,
          layers: Optional[str] = None, include_code: bool = False,
          repo: Optional[str] = None):
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
        include_code=include_code, repo=repo, backfill=False,
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
    return _run(
        service().graph_scene, workspace=ws, level=level,
        center_id=center_id, system_id=system_id, seeds=_graph_csv(seeds),
        repo=repo, layers=_graph_csv(layers), relations=_graph_csv(relations),
        entity_types=_graph_csv(entity_types), memory_types=_graph_csv(memory_types),
        as_of=as_of, time_from=time_from, time_to=time_to, depth=depth,
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
                  time_from: Optional[float] = None,
                  time_to: Optional[float] = None,
                  include_weak_cooccurrence: bool = False,
                  limit: int = Query(default=8, ge=1, le=25)):
    ws = workspace or _require_ws()
    return _run(
        service().graph_suggest, query if query is not None else q,
        workspace=ws, repo=repo, memory_types=_graph_csv(memory_types),
        as_of=as_of, time_from=time_from, time_to=time_to,
        include_weak_cooccurrence=include_weak_cooccurrence, limit=limit,
    )


@router.get("/graph/entities/{canonical_id}")
def graph_entity(canonical_id: str, workspace: Optional[str] = None,
                 repo: Optional[str] = None,
                 memory_types: Optional[str] = None,
                 as_of: Optional[float] = None,
                 time_from: Optional[float] = None,
                 time_to: Optional[float] = None,
                 include_weak_cooccurrence: bool = True):
    ws = workspace or _require_ws()
    return _run(
        service().graph_entity, canonical_id, workspace=ws, repo=repo,
        memory_types=_graph_csv(memory_types), as_of=as_of,
        time_from=time_from, time_to=time_to,
        include_weak_cooccurrence=include_weak_cooccurrence,
    )


@router.get("/graph/path")
def graph_path(source: str, target: str, workspace: Optional[str] = None,
               repo: Optional[str] = None, as_of: Optional[float] = None,
               memory_types: Optional[str] = None,
               time_from: Optional[float] = None,
               time_to: Optional[float] = None,
               max_hops: int = Query(default=8, ge=1, le=8),
               max_visits: int = Query(default=10_000, ge=1, le=50_000),
               include_weak_cooccurrence: bool = False):
    ws = workspace or _require_ws()
    return _run(
        service().graph_path, source, target, workspace=ws, repo=repo,
        as_of=as_of, memory_types=_graph_csv(memory_types),
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


@router.post("/code/index")
def code_index(req: _CodeIndexReq):
    return _run(
        service().index_repo, workspace=req.workspace, repo=req.repo,
        root_path=req.root_path, languages=req.languages,
    )


@router.get("/code/search")
def code_search(query: str, workspace: str, repo: str, limit: int = 20):
    return _run(
        service().search_code, query, workspace=workspace, repo=repo, limit=limit,
    )


class _CodePathReq(BaseModel):
    workspace: str
    repo: str
    source: str
    target: str
    max_depth: int = 8


@router.post("/code/path")
def code_path(req: _CodePathReq):
    return _run(
        service().code_path, req.source, req.target, workspace=req.workspace,
        repo=req.repo, max_depth=req.max_depth,
    )


class _CodeImpactReq(BaseModel):
    workspace: str
    repo: str
    changed_files: list[str]


@router.post("/code/impact")
def code_impact(req: _CodeImpactReq):
    return _run(
        service().code_impact, req.changed_files,
        workspace=req.workspace, repo=req.repo,
    )


@router.get("/code/export")
def code_export(workspace: str, repo: str):
    return _run(service().export_code_graph, workspace=workspace, repo=repo)


# ── license ───────────────────────────────────────────────────────────────────
class _KeyReq(BaseModel):
    key: str = Field(..., min_length=1, max_length=8192)


class _TrialReq(BaseModel):
    email: str = Field(default="", max_length=320)


class _TrialClaimReq(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    plan: str = Field(..., min_length=3, max_length=16)
    # Optional: a remote caller must still send the configured ownership token (enforced
    # in the handler), but a trusted loopback caller may omit it — the local UI does.
    deployment_token: str = Field(default="", max_length=8192)


@router.get("/license")
def get_license():
    lic = licensing.current_license(refresh=True).to_public_dict()
    lic["error"] = licensing.license_error()
    return lic


@router.post("/license/activate")
def activate_license(req: _KeyReq):
    """Verify + persist a key. A signature-valid key can still land on the free tier
    if the server-side cloud gate denies it (revoked, seat-capped, or this process
    can't reach the license server) — ``licensing.activate`` degrades silently in
    that case rather than raising, so it looked exactly like a successful
    activation of the free tier. Surface the real reason the same way
    :func:`get_license` does, so the caller can tell "activated Team" from
    "accepted the key but the cloud check failed" instead of both reading as a
    plain 200 with ``plan: free``."""
    try:
        lic = licensing.activate(req.key)
    except licensing.LicenseError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    data = lic.to_public_dict()
    data["error"] = licensing.license_error()
    return data


@router.post("/license/trial")
def start_trial(req: _TrialReq, request: Request):
    """Deprecated v1.0 wrapper for a deployment-bound Pro claim."""
    token, fallback = _trial_binding(request)
    result = _start_bound_trial(req.email, "pro", token, dashboard_url_fallback=fallback)
    result["deprecated"] = True
    result["replacement"] = "/api/license/trials"
    return result


@router.post("/license/team-trial")
def start_team_trial(req: _TrialReq, request: Request):
    """Deprecated v1.0 wrapper for a deployment-bound Team claim."""
    token, fallback = _trial_binding(request)
    result = _start_bound_trial(req.email, "team", token, dashboard_url_fallback=fallback)
    result["deprecated"] = True
    result["replacement"] = "/api/license/trials"
    return result


def _configured_deployment_token() -> str:
    return os.environ.get("ENGRAPHIS_DEPLOYMENT_TOKEN", "").strip()


def _local_deployment_token() -> str:
    """A stable, machine-bound trial token for a trusted loopback caller.

    Loopback requests are already fully trusted by the dashboard auth gate (they reach
    every ``/api`` route without a bearer, and first-admin setup accepts them the same
    way), so the deployment-token ownership proof adds no security on localhost — it only
    stops the local operator from starting a trial with a secret that, on a self-hosted
    box, nobody ever configured. Derive a deterministic value from the machine id so the
    create -> email-confirm -> poll round-trip binds to a single ``deployment_hash`` even
    across a process restart, without asking the operator to invent one.
    """
    import hashlib

    from engraphis import cloud_license
    digest = hashlib.sha256(
        ("engraphis-local-trial:" + cloud_license.machine_id()).encode("utf-8")).hexdigest()
    return "local-" + digest


def _effective_deployment_token(request: Request) -> str:
    """The configured ownership token, or a machine-bound one for trusted loopback."""
    configured = _configured_deployment_token()
    if configured:
        return configured
    return _local_deployment_token() if is_local_request(request) else ""


def _trial_binding(request: Request) -> "tuple[str, str]":
    """``(deployment_token, dashboard_url_fallback)`` for a trial from *request*.

    On loopback with nothing configured, the fallback dashboard URL is the request's own
    origin, so a purely local instance needs neither ``ENGRAPHIS_DEPLOYMENT_TOKEN`` nor
    ``ENGRAPHIS_DASHBOARD_URL`` set to start a trial. A proxied internet request never
    looks local (any ``X-Forwarded-*`` header disqualifies it), so this changes nothing
    for a hosted deployment.
    """
    local = is_local_request(request)
    token = _configured_deployment_token() or (_local_deployment_token() if local else "")
    fallback = str(request.base_url).rstrip("/") if local else ""
    return token, fallback


def _start_bound_trial(email: str, plan: str, deployment_token: str,
                       *, dashboard_url_fallback: str = "") -> dict:
    email = email.strip().lower()
    if not email or "@" not in email or len(email) > 320:
        raise HTTPException(status_code=400, detail={
            "error": "a valid email address is required to start a trial"})
    if not deployment_token:
        raise HTTPException(status_code=503, detail={
            "error": "ENGRAPHIS_DEPLOYMENT_TOKEN is not configured"})
    dashboard_url = (os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
                     or (dashboard_url_fallback or "").strip())
    if not dashboard_url:
        raise HTTPException(status_code=503, detail={
            "error": "ENGRAPHIS_DASHBOARD_URL is required for hosted trials"})
    from engraphis import cloud_license
    from engraphis.config import resolve_license_server_url
    try:
        result = cloud_license.create_trial_claim(
            resolve_license_server_url(), deployment_token, cloud_license.machine_id(),
            email, plan, dashboard_url=dashboard_url)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    result.pop("key", None)
    return result


@router.post("/license/trials")
def create_deployment_trial(req: _TrialClaimReq, request: Request):
    """Start a claim after proving ownership of this deployment.

    A remote caller must present the configured ``ENGRAPHIS_DEPLOYMENT_TOKEN`` as an
    ownership proof. A loopback caller is already trusted (see
    :func:`_local_deployment_token`), so on localhost the token and dashboard URL are
    derived automatically and a self-hosted instance can trial with nothing configured.
    """
    local = is_local_request(request)
    configured = _configured_deployment_token()
    if not configured and not local:
        raise HTTPException(status_code=503, detail={
            "error": "ENGRAPHIS_DEPLOYMENT_TOKEN is not configured"})
    if configured and not local and not hmac.compare_digest(
            configured, req.deployment_token or ""):
        raise HTTPException(status_code=401, detail={"error": "invalid deployment token"})
    plan = req.plan.strip().lower()
    if plan not in ("pro", "team"):
        raise HTTPException(status_code=400, detail={"error": "plan must be pro or team"})
    token = configured or _local_deployment_token()
    fallback = str(request.base_url).rstrip("/") if local else ""
    return _start_bound_trial(req.email, plan, token, dashboard_url_fallback=fallback)


@router.get("/license/trials/{claim_id}")
def get_deployment_trial(claim_id: str, request: Request):
    """Poll, retrieve, persist, and activate a confirmed claim without exposing its key."""
    token = _effective_deployment_token(request)
    if not token:
        raise HTTPException(status_code=503, detail={"error": "deployment token unavailable"})
    from engraphis import cloud_license
    from engraphis.config import resolve_license_server_url
    try:
        result = cloud_license.claim_trial(
            resolve_license_server_url(), claim_id, token, cloud_license.machine_id())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc)})
    key = result.pop("key", None)
    if key:
        try:
            license_public = licensing.activate(key).to_public_dict()
        except licensing.LicenseError as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)})
        result["license"] = license_public
        result["active"] = license_public.get("plan") in ("pro", "team")
    return result


@router.post("/ops/backup")
def run_customer_backup():
    """Run the configured off-volume backup; authentication is enforced by middleware."""
    try:
        from engraphis.commercial import run_configured_backup
        result = run_configured_backup()
    except Exception as exc:  # noqa: BLE001 - never expose storage paths or key detail
        import logging
        logging.getLogger("engraphis.backup").error(
            "customer backup failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            "ok": False, "verified": False})
    if not result["verified"]:
        raise HTTPException(status_code=503, detail=result)
    return result


@router.get("/ops/ready")
def customer_operations_ready():
    """Authenticated, boolean-only storage readiness for the managed customer service."""
    from engraphis.commercial import customer_operations_readiness
    from fastapi.responses import JSONResponse
    checks = customer_operations_readiness()
    return JSONResponse(checks, status_code=200 if checks["ready"] else 503)


# ── Cloud sync (Pro) — the dashboard's one-click "Sync now" button ────────────────────
# The heavy lifting is in core/sync.py + the RelayTransport client; these two routes just
# expose it to the dashboard so a user never touches a terminal. Sync is namespaced by
# workspace NAME (every device on the account shares a namespace); identity is the license
# key, verified server-side by the relay. See docs/SYNC.md.

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
    has_token = has_sync_token()
    has_key = bool(licensing._read_key_material())
    lic = licensing.current_license(refresh=False)
    return {
        # Ready only when the plan includes sync AND a key is configured. Purchased and
        # trial entitlements are both real server-issued keys.
        "available": bool(has_token or (licensing.has_feature("sync") and has_key)),
        "has_key": has_key,
        "has_user_token": has_token,
        "read_only": sync_read_only(),
        "token_managed_by_environment": bool(
            os.environ.get("ENGRAPHIS_SYNC_TOKEN", "").strip()),
        "read_only_managed_by_environment": bool(
            os.environ.get("ENGRAPHIS_SYNC_READ_ONLY", "").strip()),
        "plan": lic.plan,
        "relay_url": _relay_url(),
        "tier_required": licensing.required_plan("sync"),
        "upgrade_url": licensing.upgrade_url(),
        "last": _SYNC_STATE.get("last"),
    }


def _sync_all(svc) -> dict:
    """Push every workspace's memories to the relay and pull every peer's — the shared
    core behind both the dashboard 'Sync now' button and the background auto-sync loop.

    Never raises: a relay/transport failure on one workspace is captured in ``errors``
    (with the HTTP ``status`` when known) so a single bad workspace never aborts the rest,
    and the background loop can keep ticking. Returns the last-sync summary; the caller
    decides whether to surface an error to a human (the button) or just log it (auto)."""
    from engraphis.backends.sync_folder import get_transport
    from engraphis.backends.sync_relay import RelayError
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
    for w in wss:
        name = w.get("name")
        if not name:
            continue
        if w.get("visibility") == "personal":
            # Personal folders are private to their owner and must never leave this device
            # over the shared-account relay: a team shares one license key, so the relay is
            # namespaced per workspace but not partitioned per user — pushing a personal
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
        try:
            transport = get_transport("relay", base_url=_relay_url(), workspace_id=name)
            from engraphis.backends.sync_relay import sync_read_only
            read_only = sync_read_only()
            rep = syncer.sync(transport, row["id"], push=not read_only)
        except RelayError as exc:
            # Record the HTTP status (402 == relay rejected the key) instead of raising, so
            # one workspace can't abort the sweep; sync_run() promotes a 402 to the button.
            errors.append({"workspace": name, "error": str(exc), "status": exc.status})
            continue
        except Exception as exc:  # noqa: BLE001 — one bad workspace must not abort the rest
            logger.error("sync workspace failed (%s)", type(exc).__name__)
            errors.append({"workspace": name, "error": "sync workspace failed"})
            continue
        exported += int(rep.get("exported_memories", 0) or 0)
        for a in rep.get("applied") or []:
            dev = a.get("from_device")
            if dev and dev != "?" and "error" not in a:
                peer_devices.add(dev)
        for k in totals:
            totals[k] += int((rep.get("totals") or {}).get(k, 0) or 0)

    return {"at": time.time(), "workspaces": len(wss), "exported": exported,
            "peers": len(peer_devices), "added": totals["added"],
            "updated": totals["updated"], "unchanged": totals["unchanged"],
            "errors": errors}


@router.post("/sync/run")
async def sync_run():
    """Push this device's memories to the relay and pull every other device's — for every
    workspace. Backs the dashboard 'Sync now' button. Pro/Team; needs a license key."""
    from engraphis.backends.sync_relay import has_sync_token
    has_token = has_sync_token()
    if not has_token:
        _paid("sync")   # legacy paid-key migration path
    if not has_token and not licensing._read_key_material():
        raise HTTPException(status_code=402, detail={
            "error": "Cloud sync needs your license key. Sign in with it above, then Sync.",
            "upgrade_url": licensing.upgrade_url()})

    svc = service()
    if not (svc.list_workspaces().get("workspaces") or []):
        raise HTTPException(status_code=400,
                            detail={"error": "Nothing to sync yet — add a memory first."})

    import asyncio
    summary = await asyncio.to_thread(_sync_all, svc)
    _SYNC_STATE["last"] = summary
    # If the relay rejected the key for every workspace (nothing exported, a 402 seen),
    # surface it as the button's upgrade/renew prompt rather than a silent partial success.
    if summary["exported"] == 0 and any(e.get("status") == 402 for e in summary["errors"]):
        first = next(e for e in summary["errors"] if e.get("status") == 402)
        raise HTTPException(status_code=402, detail={
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
                save_sync_token(req.token)
            if not req.read_only:
                save_sync_read_only(False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
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


class _AutoSyncReq(BaseModel):
    enabled: Optional[bool] = None            # cadence (timer) sync
    cadence_minutes: Optional[int] = None


@router.get("/sync/auto")
def sync_auto_get():
    """The background auto-sync policy for the dashboard toggle (cadence + last run).
    License-ungated read (it only reports a local preference and defaults to off); in team
    mode the dashboard's role gate still limits it to signed-in users, and only admins can
    POST changes (the toggle renders read-only for members/viewers)."""
    from engraphis import autosync
    return autosync.load_policy()


@router.post("/sync/auto")
def sync_auto_set(req: _AutoSyncReq):
    """Enable/disable background cadence auto-sync and set its interval (minutes, floored at
    5). Pro/Team — the same ``sync`` feature the button needs. In team mode this route is
    **admin-only** (``inspector/auth.min_role``): auto-sync is an account-wide control.
    The loop itself is licensed-gated too, so a stale toggle can never reach the relay
    after a plan lapses."""
    from engraphis.backends.sync_relay import has_sync_token
    if not has_sync_token():
        _paid("sync")
    from engraphis import autosync
    cur = autosync.load_policy()
    merged = {k: (getattr(req, k) if getattr(req, k) is not None else cur.get(k))
              for k in ("enabled", "cadence_minutes")}
    return autosync.save_policy(merged)
