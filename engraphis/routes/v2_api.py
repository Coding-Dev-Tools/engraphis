"""v1-dashboard API adapter over the v2 MemoryService (engraphis/service.py).

Serves the restored v1 dashboard on the *v2* engine — same look, real (v2) data.
Every route is under /api and returns plain JSON the dashboard's JS consumes. Paid
surfaces (analytics, export) gate through engraphis.licensing. Team auth lives in
engraphis/routes/v2_team.py and is included by the dashboard app.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from engraphis import licensing
from engraphis.config import DEFAULT_RELAY_URL, settings
from engraphis.service import MemoryService, ValidationError

router = APIRouter(prefix="/api", tags=["dashboard"])

_service: Optional[MemoryService] = None


def service() -> MemoryService:
    """Lazily bind a single MemoryService to the configured store (the live v2 DB)."""
    global _service
    if _service is None:
        _service = MemoryService.create(
            settings.db_path, embed_model=settings.embed_model,
            embed_dim=settings.embed_dim or 256)
    return _service


def set_service(svc: MemoryService) -> None:
    """Inject a service (tests / the dashboard app)."""
    global _service
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
        raise HTTPException(status_code=500, detail={"error": msg})


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
            sql += " AND (" + " OR ".join(["title LIKE ? OR content LIKE ?" for _ in terms]) + ")"
            for t in terms:
                args += ["%" + t + "%", "%" + t + "%"]
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
@router.get("/health")
def health():
    return {"status": "ok", "engine": "v2"}


@router.get("/bootstrap")
def bootstrap():
    lic = licensing.current_license(refresh=True).to_public_dict()
    lic["error"] = licensing.license_error()
    wss = _run(service().list_workspaces).get("workspaces") or []
    emb = None
    try:
        from engraphis.backends import embedder_st as _est
        e = service().engine.embedder
        d = int(getattr(e, "dim", 0))
        emb = {"class": type(e).__name__, "dim": d, "semantic": d >= 384,
               "model": settings.embed_model, "error": getattr(_est, "LAST_EMBEDDER_ERROR", "")}
    except Exception:  # noqa: BLE001
        pass
    return {"license": lic, "workspaces": wss, "stats": _run(service().stats), "embedder": emb}


# ── workspaces / stats ────────────────────────────────────────────────────────
@router.get("/workspaces")
def workspaces():
    return _run(service().list_workspaces)


class _CreateWsReq(BaseModel):
    workspace: str
    description: str = ""


@router.post("/workspaces/create")
def workspaces_create(req: _CreateWsReq):
    """Create an empty workspace/folder up front (see MemoryService.create_workspace).
    In team mode this is a POST, so the dashboard's auth gate requires the member role or
    above — viewers can't create folders, members and admins can, and every folder is
    shared with the whole team."""
    return _run(service().create_workspace, req.workspace, req.description)


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


@router.post("/workspaces/import-folder")
def workspaces_import_folder(req: _ImportFolderReq):
    """Import files from a directory on the machine running Engraphis into ``workspace``,
    one memory per file (see MemoryService.import_folder, SECURITY.md §5). POST defaults
    to the member role in min_role() — same as workspaces/create — so a viewer can't
    trigger a disk read; the traversal guard itself lives in the service layer."""
    return _run(service().import_folder, workspace=req.workspace, path=req.path,
                file_pattern=req.file_pattern, memory_type=req.memory_type)


@router.post("/workspaces/import-files")
async def workspaces_import_files(workspace: str = Form(...),
                                  memory_type: str = Form("semantic"),
                                  files: list[UploadFile] = File(...)):
    """Drag-and-drop / picked-file upload counterpart to import-folder (see
    MemoryService.import_files). Each upload is read bounded by
    ``MemoryService.MAX_IMPORT_FILE_BYTES`` before decoding — a resource bound, not a
    security boundary (see that constant's docstring); the rest of validation is
    transport-agnostic and lives in the service layer, same as every other write."""
    from engraphis.service import MAX_IMPORT_FILE_BYTES
    payload = []
    for f in files:
        raw = await f.read(MAX_IMPORT_FILE_BYTES)
        payload.append({"name": f.filename or "untitled",
                        "content": raw.decode("utf-8", errors="replace")})
    return _run(service().import_files, workspace=workspace, files=payload,
                memory_type=memory_type)


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
            raise HTTPException(status_code=500, detail={"error": str(exc)})
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
            sql += " AND (title LIKE ? OR content LIKE ?)"
            like = "%" + q + "%"
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
            raise HTTPException(status_code=500, detail={"error": str(exc)})
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
            raise HTTPException(status_code=500, detail={"error": str(exc)})
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


@router.get("/audit")
def audit(workspace: Optional[str] = None, limit: int = 100):
    ws = workspace or _require_ws()
    return _run(service().audit_log, workspace=ws, limit=limit)


# ── governance: pin / forget / correct ───────────────────────────────────────
class _IdReq(BaseModel):
    id: str
    workspace: Optional[str] = None
    reason: str = ""
    pinned: bool = True
    content: str = ""


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


# ── consolidate ───────────────────────────────────────────────────────────────
class _ConsolidateReq(BaseModel):
    workspace: Optional[str] = None
    dry_run: bool = True


@router.post("/consolidate")
def consolidate(req: _ConsolidateReq):
    ws = req.workspace or _default_ws()
    return _run(service().consolidate, workspace=ws, dry_run=req.dry_run)


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
    licensing.require_feature("analytics")
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
                        "archive_below", "workspaces")}
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
def graph(workspace: Optional[str] = None, limit: int = 2000):
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
    return _run(service().graph, workspace=ws, limit=limit)


# ── license ───────────────────────────────────────────────────────────────────
class _KeyReq(BaseModel):
    key: str


@router.get("/license")
def get_license():
    lic = licensing.current_license(refresh=True).to_public_dict()
    lic["error"] = licensing.license_error()
    return lic


@router.post("/license/activate")
def activate_license(req: _KeyReq):
    try:
        lic = licensing.activate(req.key)
    except licensing.LicenseError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    return lic.to_public_dict()


@router.post("/license/trial")
def start_trial():
    """Begin the one-time local free trial (unlocks every Pro feature for the trial
    window, no key required). 400 if a paid license is active or the trial is spent."""
    try:
        return licensing.start_trial()
    except licensing.LicenseError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})


@router.post("/license/team-trial")
def start_team_trial():
    """Begin the one-time self-serve Team trial (unlocks Team mode, seats, and the
    invite-email relay — a real signed key from the vendor, no purchase). One
    network round-trip to the relay, unlike the fully-offline Pro trial above; see
    ``licensing.start_team_trial``. 400 if a paid license is active, this device's
    trial was already claimed, or the relay is unreachable."""
    try:
        return licensing.start_team_trial()
    except licensing.LicenseError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})


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
    return (settings.relay_url or DEFAULT_RELAY_URL).rstrip("/")


@router.get("/sync/status")
def sync_status():
    """Whether one-click cloud sync is ready, plus the last-sync summary for the button."""
    has_key = bool(licensing._read_key_material())
    lic = licensing.current_license(refresh=False)
    return {
        # Ready only when the plan includes sync AND a key is configured (the relay needs
        # a real key; a local trial alone can't reach it — it stays folder-sync only).
        "available": bool(licensing.has_feature("sync") and has_key),
        "has_key": has_key,
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
        row = svc.store.conn.execute(
            "SELECT id FROM workspaces WHERE name=?", (name,)).fetchone()
        if not row:
            continue
        try:
            transport = get_transport("relay", base_url=_relay_url(), workspace_id=name)
            rep = syncer.sync(transport, row["id"])
        except RelayError as exc:
            # Record the HTTP status (402 == relay rejected the key) instead of raising, so
            # one workspace can't abort the sweep; sync_run() promotes a 402 to the button.
            errors.append({"workspace": name, "error": str(exc), "status": exc.status})
            continue
        except Exception as exc:  # noqa: BLE001 — one bad workspace must not abort the rest
            errors.append({"workspace": name, "error": str(exc)})
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
def sync_run():
    """Push this device's memories to the relay and pull every other device's — for every
    workspace. Backs the dashboard 'Sync now' button. Pro/Team; needs a license key."""
    _paid("sync")   # 402 if the plan doesn't include sync
    if not licensing._read_key_material():
        raise HTTPException(status_code=402, detail={
            "error": "Cloud sync needs your license key. Sign in with it above, then Sync.",
            "upgrade_url": licensing.upgrade_url()})

    svc = service()
    if not (svc.list_workspaces().get("workspaces") or []):
        raise HTTPException(status_code=400,
                            detail={"error": "Nothing to sync yet — add a memory first."})

    summary = _sync_all(svc)
    _SYNC_STATE["last"] = summary
    # If the relay rejected the key for every workspace (nothing exported, a 402 seen),
    # surface it as the button's upgrade/renew prompt rather than a silent partial success.
    if summary["exported"] == 0 and any(e.get("status") == 402 for e in summary["errors"]):
        first = next(e for e in summary["errors"] if e.get("status") == 402)
        raise HTTPException(status_code=402, detail={
            "error": first["error"], "upgrade_url": licensing.upgrade_url()})
    return {"ok": True, "summary": summary}


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
    _paid("sync")
    from engraphis import autosync
    cur = autosync.load_policy()
    merged = {k: (getattr(req, k) if getattr(req, k) is not None else cur.get(k))
              for k in ("enabled", "cadence_minutes")}
    return autosync.save_policy(merged)
