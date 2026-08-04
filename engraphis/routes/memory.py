"""All Engraphis-compatible API routes, mounted under /memory.

Every route returns {"data": ...} to match the upstream SDK contract
(the Python SDK does ``payload["data"]`` on every response).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form

from engraphis.engines import ingest as ingest_engine
from engraphis.engines import recall as recall_engine
from engraphis.engines import reweight, thoughts as thoughts_engine
from engraphis.llm.client import LLMClient
from engraphis.models import (
    BatchDocumentsRequest,
    ChatRequest,
    DeleteMemoryRequest,
    DocumentItem,
    InsertDocumentRequest,
    InsertMemoryRequest,
    InteractionRequest,
    MemoryItem,
    PruneRequest,
    QueryContextRequest,
    QueryMemoryRequest,
    RecallMasterRequest,
    RecallMemoriesRequest,
    ReinforceRequest,
    ThoughtRequest,
)
from engraphis.stores import graph as graph_store
from engraphis.stores import ledger as ledger_store
from engraphis.stores import vectors as mem_store
from engraphis import licensing
from engraphis.core.store import _escape_like
from engraphis.core.secrets import SecretDetectedError
from pydantic import BaseModel

logger = logging.getLogger("engraphis.routes")
router = APIRouter(prefix="/memory", tags=["memory"])


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data}


def _safe_call(fn, *args, **kwargs):
    """Map route-facing failures to stable, content-free HTTP errors.

    The legacy engines predate FastAPI and can raise provider, SQLite, or secret
    detector exceptions directly. Never let those implementation details become
    an API response; validation remains a client error while all other failures
    use one generic server error.
    """
    try:
        return fn(*args, **kwargs)
    except SecretDetectedError:
        raise HTTPException(status_code=400, detail={"error": "memory content rejected"}) from None
    except HTTPException as exc:
        status = exc.status_code if isinstance(exc.status_code, int) else 500
        if 400 <= status <= 499:
            raise HTTPException(status_code=status, detail={"error": "request rejected"}) from None
        raise HTTPException(status_code=500, detail={"error": "internal server error"}) from None
    except (TypeError, ValueError) as exc:
        logger.info("memory route validation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None
    except Exception as exc:  # noqa: BLE001 - legacy providers expose varied exception types
        logger.error("memory route operation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"error": "internal server error"}) from None


def _bounded_count(value: Optional[int], *, field: str, default: int,
                   maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise HTTPException(
            status_code=400,
            detail={"error": f"{field} must be between 1 and {maximum}"},
        )
    return value



def _norm_doc_id(item: DocumentItem) -> str:
    return item.document_id or item.documentId or f"doc-{int(time.time()*1000)}"


# ── Core memory routes (legacy insert/query/delete) ─────────────────────────

@router.post("/insert")
async def insert_memory(req: InsertMemoryRequest):
    """POST /memory/insert — upsert a single memory (key → documentId)."""
    if req.item:
        item = req.item
    else:
        if not (req.key and req.content and req.namespace):
            raise HTTPException(400, "key, content, namespace are required (or pass item)")
        item = MemoryItem(
            key=req.key, content=req.content, namespace=req.namespace,
            metadata=req.metadata or {}, created_at=req.created_at, updated_at=req.updated_at,
        )
    result = _safe_call(
        ingest_engine.ingest_document,
        namespace=item.namespace,
        document_id=item.key,
        title=item.key,
        content=item.content,
        metadata=item.metadata,
        created_at=item.created_at,
        updated_at=item.updated_at,
        memory_type=req.memory_type or req.memoryType or "semantic",
    )
    status = "updated" if result.get("access_count", 0) > 0 else "inserted"
    return _ok({"status": status, "ingested": 1 if status == "inserted" else 0,
                "updated": 1 if status == "updated" else 0, "errors": 0,
                "jobId": result.get("jobId")})


@router.post("/query")
async def query_memory(req: QueryMemoryRequest):
    """POST /memory/query — recall context for an LLM prompt."""
    prompt = req.query or req.prompt
    if not prompt:
        raise HTTPException(400, "query or prompt is required")
    doc_ids = req.documentIds or req.keys
    if req.key and not doc_ids:
        doc_ids = [req.key]
    result = _safe_call(
        recall_engine.recall,
        namespace=req.namespace,
        prompt=prompt,
        num_chunks=_bounded_count(
            req.maxChunks if req.maxChunks is not None else req.num_chunks,
            field="maxChunks", default=10, maximum=100,
        ),
        document_ids=doc_ids,
    )
    return _ok(result)


@router.post("/admin/delete")
async def delete_memory(req: DeleteMemoryRequest):
    """POST /memory/admin/delete — delete a namespace (must confirm with delete_all=True)."""
    confirm = req.delete_all or (req.deleteAll or False)
    if not confirm:
        raise HTTPException(400, "Set delete_all=True to confirm namespace deletion")
    count = _safe_call(mem_store.delete_namespace, req.namespace)
    return _ok({"deleted": count, "nodesDeleted": count})


# ── Documents routes ─────────────────────────────────────────────────────────

@router.post("/documents")
async def insert_document(req: InsertDocumentRequest):
    """POST /memory/documents — insert a single document."""
    doc_id = _norm_doc_id(req)
    result = _safe_call(
        ingest_engine.ingest_document,
        namespace=req.namespace,
        document_id=doc_id,
        title=req.title,
        content=req.content,
        metadata=req.metadata,
        source_type=req.source_type or req.sourceType,
        priority=req.priority,
        created_at=req.created_at or req.createdAt,
        updated_at=req.updated_at or req.updatedAt,
    )
    return _ok(result)


@router.post("/documents/batch")
async def insert_documents_batch(req: BatchDocumentsRequest):
    """POST /memory/documents/batch — insert multiple documents."""
    items = []
    for it in req.items:
        items.append({
            "namespace": it.namespace,
            "document_id": _norm_doc_id(it),
            "title": it.title,
            "content": it.content,
            "metadata": it.metadata,
            "sourceType": it.source_type or it.sourceType,
            "priority": it.priority,
            "createdAt": it.created_at or it.createdAt,
            "updatedAt": it.updated_at or it.updatedAt,
        })
    result = _safe_call(ingest_engine.ingest_batch, items)
    return _ok(result)


@router.get("/documents")
async def list_documents(namespace: Optional[str] = None,
                         limit: Optional[int] = Query(default=None, ge=1, le=10_000),
                         offset: Optional[int] = Query(default=None, ge=0, le=1_000_000)):
    """GET /memory/documents — list documents."""
    docs = _safe_call(mem_store.list_documents, namespace=namespace, limit=limit, offset=offset)
    return _ok({"documents": docs, "count": len(docs)})


@router.get("/documents/{document_id}")
async def get_document(document_id: str, namespace: Optional[str] = None):
    """GET /memory/documents/{documentId} — get a single document. Without ``namespace``,
    look it up across all namespaces instead of a nonexistent ``_global`` one (which made
    the query always 404)."""
    doc = _safe_call(mem_store.find_document, document_id, namespace)
    if not doc:
        raise HTTPException(404, f"Document {document_id} not found")
    return _ok(doc)


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, namespace: str = Query(...)):
    """DELETE /memory/documents/{documentId} — delete a single document."""
    count = _safe_call(mem_store.delete_memory_document, document_id, namespace)
    return _ok({"deleted": count, "documentId": document_id})


# ── Queries / conversations (mirrored endpoints) ────────────────────────────

@router.post("/queries")
async def query_memory_context(req: QueryContextRequest):
    """POST /memory/queries — query memory context with optional LLM."""
    doc_ids = req.documentIds or req.document_ids
    if not req.query.strip():
        raise HTTPException(status_code=400, detail={"error": "query is required"})
    result = _safe_call(
        recall_engine.recall,
        namespace=req.namespace,
        prompt=req.query,
        num_chunks=_bounded_count(req.maxChunks, field="maxChunks", default=10, maximum=100),
        document_ids=doc_ids,
    )
    if req.recallOnly:
        return _ok(result)
    if req.llmQuery or req.query:
        import asyncio
        try:
            def _call():
                with LLMClient() as llm:
                    return llm.chat_with_context(
                        user_prompt=req.llmQuery or req.query,
                        context=result.get("llmContextMessage", ""),
                    )
            answer = await asyncio.to_thread(_call)
            result["answer"] = answer
        except Exception as exc:  # noqa: BLE001 - provider libraries expose many exception types
            logger.warning("LLM query error (%s)", type(exc).__name__)
            result["llm_error"] = "LLM service unavailable"
    return _ok(result)


@router.post("/conversations")
async def chat_memory_context(req: ChatRequest):
    """POST /memory/conversations — chat with memory context."""
    user_msg = next((m for m in reversed(req.messages) if m.get("role") == "user"), None)
    if not user_msg:
        raise HTTPException(400, "At least one user message is required")
    user_content = user_msg.get("content")
    if not user_content or not str(user_content).strip():
        raise HTTPException(400, "The latest user message must have non-empty 'content'")
    ctx = _safe_call(recall_engine.recall, namespace=None, prompt=user_content, num_chunks=10)
    import asyncio
    try:
        def _call():
            with LLMClient() as llm:
                return llm.chat_with_context(
                    user_prompt=user_content,
                    context=ctx.get("llmContextMessage", ""),
                    temperature=req.temperature,
                    max_tokens=req.maxTokens or req.max_tokens,
                )
        answer = await asyncio.to_thread(_call)
    except Exception as exc:
        # Some provider errors include a credentialed request URL. The client already
        # receives a generic response, so keep the log equally content-free.
        logger.warning("LLM chat error (%s)", type(exc).__name__)
        raise HTTPException(500, "LLM service unavailable")
    return _ok({"answer": answer, "context": ctx.get("chunks", []), "context_count": ctx["count"]})


# ── Interactions ─────────────────────────────────────────────────────────────

@router.post("/interactions")
async def record_interactions(req: InteractionRequest):
    """POST /memory/interactions — record interaction signals."""
    names = req.entityNames or req.entity_names or []
    if not names:
        raise HTTPException(400, "entityNames is required")
    levels = req.interactionLevels or req.interaction_levels
    level = req.interactionLevel or req.interaction_level or (levels[0] if levels else "view")
    reinforced = 0
    for name in names:
        _safe_call(
            ledger_store.record_interaction,
            namespace=req.namespace,
            entity_name=name,
            interaction_level=level,
            description=req.description,
            timestamp=req.timestamp,
        )
        # Actually reinforce memories mentioning the entity — otherwise the signal is only
        # logged and never affects retention.
        reinforced += _safe_call(
            reweight.boost_entity_memories, req.namespace, name, level
        )
    return _ok({"recorded": len(names), "namespace": req.namespace, "level": level,
                "memories_reinforced": reinforced})


@router.post("/interact")
async def interact_memory(req: InteractionRequest):
    """POST /memory/interact — mirrored interaction recording."""
    return await record_interactions(req)


@router.post("/reinforce")
async def reinforce_memory(req: ReinforceRequest):
    """POST /memory/reinforce — reinforce a specific memory by document ID.

    Increases stability (spacing effect) and updates last_access, preventing
    Ebbinghaus decay. Use when an agent finds a past memory useful for current work.
    """
    namespace = req.namespace or "default"
    mem = _safe_call(mem_store.get_memory, namespace, req.documentId)
    if not mem:
        raise HTTPException(404, f"Document {req.documentId} not found in namespace {namespace}")
    _safe_call(reweight.reinforce, mem["id"])
    return _ok({"reinforced": True, "documentId": req.documentId, "namespace": namespace})


@router.post("/prune")
async def prune_memory(req: PruneRequest):
    """POST /memory/prune — delete decayed memories below a retention threshold."""
    from engraphis.engines.reweight import retention_score

    if req.min_retention is not None:
        threshold = req.min_retention
    elif req.minRetention is not None:
        threshold = req.minRetention
    else:
        threshold = 0.05
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = math.nan
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise HTTPException(
            status_code=400,
            detail={"error": "minRetention must be between 0 and 1"},
        )
    dry_run = req.dry_run if req.dry_run is not None else bool(req.dryRun)
    keep_pinned = req.keepPinned if req.keepPinned is not None else True
    max_delete = req.maxDelete if req.maxDelete is not None else 500
    if (isinstance(max_delete, bool) or not isinstance(max_delete, int)
            or not 0 <= max_delete <= 10_000):
        raise HTTPException(
            status_code=400,
            detail={"error": "maxDelete must be between 0 and 10000"},
        )

    candidates = []
    for mem in _safe_call(mem_store.list_documents, namespace=req.namespace, limit=100000):
        if keep_pinned and (mem.get("metadata") or {}).get("pinned"):
            continue
        r = retention_score(mem)
        if r < threshold:
            candidates.append({
                "documentId": mem["document_id"],
                "retention": round(r, 4),
                "memoryType": mem.get("memory_type"),
                "title": mem.get("title"),
            })
    candidates.sort(key=lambda c: c["retention"])
    candidates = candidates[:max_delete]

    deleted = 0
    if not dry_run:
        for c in candidates:
            deleted += _safe_call(
                mem_store.delete_memory_document, c["documentId"], req.namespace
            )

    return _ok({
        "namespace": req.namespace,
        "threshold": threshold,
        "dryRun": dry_run,
        "matched": len(candidates),
        "deleted": deleted,
        "pruned": candidates[:50],
    })


# ── Thoughts / recall ────────────────────────────────────────────────────────

@router.post("/memories/thoughts")
async def recall_thoughts(req: ThoughtRequest):
    result = _safe_call(
        thoughts_engine.synthesize_thoughts,
        namespace=req.namespace,
        max_chunks=_bounded_count(
            req.maxChunks if req.maxChunks is not None else req.max_chunks,
            field="maxChunks", default=10, maximum=100,
        ),
        temperature=req.temperature,
        randomness_seed=req.randomnessSeed or req.randomness_seed,
        persist=req.persist if req.persist is not None else True,
        thought_prompt=req.thoughtPrompt or req.thought_prompt,
    )
    return _ok(result)


@router.post("/memories/recall")
async def recall_memories(req: RecallMemoriesRequest):
    result = _safe_call(
        recall_engine.recall_by_retention,
        namespace=req.namespace,
        top_k=_bounded_count(
            req.topK if req.topK is not None else req.top_k,
            field="topK", default=10, maximum=100,
        ),
        min_retention=(
            req.minRetention if req.minRetention is not None
            else req.min_retention if req.min_retention is not None else 0.0
        ),
        as_of=req.asOf or req.as_of,
    )
    return _ok(result)


@router.post("/memories/context")
async def memories_context(namespace: Optional[str] = None,
                           maxChunks: Optional[int] = Query(default=10, ge=1, le=100)):
    """POST /memory/memories/context — recall context across all namespaces when unset."""
    result = _safe_call(
        recall_engine.recall_master, namespace=namespace, max_chunks=maxChunks
    )
    return _ok(result)


@router.post("/recall")
async def recall_master(req: RecallMasterRequest):
    """POST /memory/recall — recall from master node (highest retention)."""
    result = _safe_call(
        recall_engine.recall_master,
        namespace=req.namespace,
        max_chunks=_bounded_count(
            req.maxChunks if req.maxChunks is not None else req.max_chunks,
            field="maxChunks", default=10, maximum=100,
        ),
    )
    return _ok(result)


@router.post("/chat")
async def chat_memory(req: ChatRequest):
    """POST /memory/chat — chat with memory."""
    return await chat_memory_context(req)


# ── Admin / graph ────────────────────────────────────────────────────────────

@router.get("/admin/graph-snapshot")
async def graph_snapshot(namespace: Optional[str] = None, mode: Optional[str] = None,
                         limit: int = Query(default=200, ge=1, le=5_000),
                         seed_limit: int = Query(default=10, ge=0, le=100)):
    """GET /memory/admin/graph-snapshot — entity/relation graph snapshot."""
    snap = _safe_call(
        graph_store.graph_snapshot, namespace=namespace, limit=limit, seed_limit=seed_limit
    )
    return _ok(snap)


@router.get("/entity/{entity_name}/memories")
async def entity_memories(entity_name: str, namespace: Optional[str] = None,
                          limit: int = Query(default=20, ge=1, le=50)):
    """GET /memory/entity/{name}/memories — every memory behind a knowledge-graph node.

    Powers the dashboard's graph drill-down: click an entity, see (and open) the
    memories that mention it. Two match passes, deduped: ingest-event linkage first
    (precise — the payload recorded which document produced the entity), then a broad
    content-mention scan. Also returns the entity's edges for the relation panel.
    """
    import json as _json

    from engraphis.engines.reweight import retention_score
    from engraphis.stores import get_conn

    name = (entity_name or "").strip()
    if not name or len(name) > 200:
        raise HTTPException(400, "invalid entity name")
    limit = int(limit)
    conn = get_conn()

    seen: set = set()
    out: list = []

    def _add(ns: str, did: Optional[str]) -> None:
        if not did or (ns, did) in seen or len(out) >= limit:
            return
        seen.add((ns, did))
        row = conn.execute(
            "SELECT namespace, document_id, title, content, memory_type, stability, "
            "last_access, updated_at, access_count FROM memories "
            "WHERE namespace=? AND document_id=?", (ns, did)).fetchone()
        if row:
            m = dict(row)
            m["retention"] = round(retention_score(m), 4)
            m["preview"] = (m.pop("content") or "")[:240]
            out.append(m)

    # 1) precise: ingest events that recorded this entity alongside its document
    ev_sql = "SELECT namespace, payload FROM events WHERE entity_name=?"
    ev_params: list = [name]
    if namespace:
        ev_sql += " AND namespace=?"
        ev_params.append(namespace)
    for r in conn.execute(ev_sql + " ORDER BY timestamp DESC LIMIT 100", ev_params):
        try:
            _add(r["namespace"], _json.loads(r["payload"] or "{}").get("document_id"))
        except Exception as exc:
            logger.debug("Entity event payload parse skipped (%s)", type(exc).__name__)

    # 2) broad: memories whose content mentions the entity
    m_sql = "SELECT namespace, document_id FROM memories WHERE content LIKE ? ESCAPE '\\'"
    m_params: list = [f"%{_escape_like(name)}%"]
    if namespace:
        m_sql += " AND namespace=?"
        m_params.append(namespace)
    m_params.append(limit * 2)
    for r in conn.execute(m_sql + " ORDER BY updated_at DESC LIMIT ?", m_params):
        _add(r["namespace"], r["document_id"])

    e_sql = ("SELECT source_entity, target_entity, relation, weight FROM edges "
             "WHERE (source_entity=? OR target_entity=?)")
    e_params: list = [name, name]
    if namespace:
        e_sql += " AND namespace=?"
        e_params.append(namespace)
    edges = [dict(r) for r in conn.execute(e_sql + " ORDER BY weight DESC LIMIT 20", e_params)]

    return _ok({"entity": name, "namespace": namespace, "count": len(out),
                "memories": out, "edges": edges})


# ── Ingestion jobs ───────────────────────────────────────────────────────────

@router.get("/ingestion/jobs/{job_id}")
async def get_ingestion_job(job_id: str):
    """GET /memory/ingestion/jobs/{jobId} — get job status."""
    job = _safe_call(ledger_store.get_job, job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return _ok(job)


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def memory_health():
    """GET /memory/health — server health check."""
    return _ok({"status": "ok", "timestamp": time.time(), "service": "engraphis"})


# ── Dashboard support endpoints ──────────────────────────────────────────────

@router.get("/stats")
async def memory_stats():
    """GET /memory/stats — aggregate statistics for the dashboard."""
    from engraphis.stores import get_conn
    from engraphis.engines.reweight import retention_score
    conn = get_conn()

    mem_count = conn.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
    entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
    edge_count = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
    event_count = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
    thought_count = conn.execute("SELECT COUNT(*) as c FROM thoughts").fetchone()["c"]
    interaction_count = conn.execute("SELECT COUNT(*) as c FROM interactions").fetchone()["c"]

    ns_rows = conn.execute(
        "SELECT namespace, COUNT(*) as c FROM memories GROUP BY namespace ORDER BY c DESC"
    ).fetchall()
    namespaces = [{"namespace": r["namespace"], "count": r["c"]} for r in ns_rows]

    all_mems = mem_store.list_documents(limit=10000)
    retentions = [retention_score(m) for m in all_mems]
    avg_retention = sum(retentions) / len(retentions) if retentions else 0

    recent_mems = mem_store.list_documents(limit=5)

    all_events = conn.execute(
        "SELECT * FROM events ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()
    recent_events = [dict(r) for r in all_events]

    all_thoughts = conn.execute(
        "SELECT * FROM thoughts ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    recent_thoughts = [dict(r) for r in all_thoughts]

    return _ok({
        "memories": mem_count,
        "entities": entity_count,
        "edges": edge_count,
        "events": event_count,
        "thoughts": thought_count,
        "interactions": interaction_count,
        "namespaces": namespaces,
        "avg_retention": round(avg_retention, 4),
        "recent_memories": recent_mems,
        "recent_events": recent_events,
        "recent_thoughts": recent_thoughts,
    })


@router.get("/namespaces")
async def list_namespaces():
    """GET /memory/namespaces — all namespaces with counts."""
    from engraphis.stores import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT namespace, COUNT(*) as count FROM memories GROUP BY namespace ORDER BY count DESC"
    ).fetchall()
    return _ok([dict(r) for r in rows])


@router.get("/search")
async def search_documents(q: str = Query(..., min_length=1, max_length=1_000),
                           namespace: Optional[str] = None,
                           limit: int = Query(default=50, ge=1, le=1_000)):
    """GET /memory/search — full-text search across document content/titles."""
    from engraphis.stores import get_conn
    conn = get_conn()
    pattern = f"%{_escape_like(q)}%"
    if namespace:
        rows = conn.execute(
            "SELECT * FROM memories WHERE namespace=? AND (content LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
            "ORDER BY updated_at DESC LIMIT ?",
            (namespace, pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE content LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
    from engraphis.stores.vectors import _row_to_mem
    results = [_row_to_mem(r) for r in rows]
    return _ok({"results": results, "count": len(results), "query": q})


@router.get("/timeline")
async def get_timeline(namespace: Optional[str] = None,
                        limit: int = Query(default=100, ge=1, le=1_000)):
    """GET /memory/timeline — chronological event feed."""
    from engraphis.stores import get_conn
    import json
    conn = get_conn()
    if namespace:
        rows = conn.execute(
            "SELECT * FROM events WHERE namespace=? ORDER BY timestamp DESC LIMIT ?",
            (namespace, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    events = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            # Old databases may contain a malformed event payload; preserve the
            # timeline shape rather than turning one corrupt row into a 500.
            d["payload"] = {}
        events.append(d)
    return _ok({"events": events, "count": len(events)})


@router.get("/thoughts")
async def list_thoughts(namespace: Optional[str] = None,
                         limit: int = Query(default=50, ge=1, le=1_000)):
    """GET /memory/thoughts — list synthesized thoughts."""
    from engraphis.stores import get_conn
    import json
    conn = get_conn()
    if namespace:
        rows = conn.execute(
            "SELECT * FROM thoughts WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
            (namespace, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM thoughts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    thoughts = []
    for r in rows:
        d = dict(r)
        try:
            d["source_memory_ids"] = json.loads(d.get("source_memory_ids") or "[]")
        except Exception:
            d["source_memory_ids"] = []
        try:
            d["parsed"] = json.loads(d["content"])
        except Exception:
            d["parsed"] = None
        thoughts.append(d)
    return _ok({"thoughts": thoughts, "count": len(thoughts)})


@router.get("/config")
async def get_config():
    """GET /memory/config — current server configuration (keys redacted)."""
    from engraphis.config import settings
    return _ok({
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_api_key_set": bool(settings.llm_api_key),
        "llm_custom_base_url_set": bool(settings.llm_base_url),
        "embed_model": settings.embed_model,
        "loop_interval": settings.loop_interval,
        "decay_halflife_days": settings.decay_halflife_days,
    })


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    namespace: str = Form(...),
    title: Optional[str] = Form(None),
    document_id: Optional[str] = Form(None),
    source_type: str = Form("upload"),
):
    """POST /memory/documents/upload — ingest a file (multipart form data)."""
    import time as _time
    from engraphis.models import MAX_CONTENT_CHARS, _CONTROL_RE
    raw = file.file.read(MAX_CONTENT_CHARS + 1)
    if len(raw) > MAX_CONTENT_CHARS:
        raise HTTPException(413, f"File exceeds {MAX_CONTENT_CHARS} bytes")
    content = _CONTROL_RE.sub("", raw.decode("utf-8", errors="replace"))
    if not content.strip():
        raise HTTPException(400, "File is empty or could not be decoded as text")
    doc_title = title or file.filename or "upload"
    doc_id = document_id or f"upload-{int(_time.time()*1000)}"
    result = ingest_engine.ingest_document(
        namespace=namespace,
        document_id=doc_id,
        title=doc_title,
        content=content,
        source_type=source_type,
        metadata={"filename": file.filename, "content_type": file.content_type},
        trusted=False,
    )
    return _ok(result)


@router.get("/interactions")
async def list_interactions(namespace: Optional[str] = None, limit: int = 100):
    """GET /memory/interactions — list interaction signals."""
    from engraphis.stores import get_conn
    conn = get_conn()
    if namespace:
        rows = conn.execute(
            "SELECT * FROM interactions WHERE namespace=? ORDER BY timestamp DESC LIMIT ?",
            (namespace, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM interactions ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return _ok({"interactions": [dict(r) for r in rows], "count": len(rows)})


@router.get("/analytics")
async def memory_analytics():
    """GET /memory/analytics — time-series and distribution data for charts."""
    raise HTTPException(
        status_code=501,
        detail="Analytics are available through the Engraphis Cloud dashboard.",
    )


# ═══ HOSTED PLAN METADATA (the local core has no commercial authority) ══════

@router.get("/license")
async def get_license():
    """GET /memory/license — the same hosted-plan answer ``/api/license`` reports.

    This surface used to hardcode ``plan: "local", features: []``. That describes the
    *local core*, not the customer: a paying Team installation was told it was on the free
    plan here while ``/api/license`` reported Team, so two endpoints of one product
    disagreed about what had been bought. The response keys are unchanged — this stays the
    legacy ``{"data": ...}`` SDK shape with the same trial and grace disclosure — and only
    the two fields that were lying now tell the truth.

    Resolution is local and non-blocking (see ``v2_api._plan_entitlement``): no network
    call happens on this path, and an unconnected installation still reports ``local``
    with no features, exactly as before. The plan stays presentation only; Engraphis Cloud
    remains the sole authority for every paid operation.
    """
    from engraphis.routes.v2_api import hosted_plan_summary

    summary = hosted_plan_summary()
    return _ok({
        "plan": summary["plan"],
        "features": summary["features"],
        "cloud_managed": True,
        "trial_seconds": licensing.TRIAL_SECONDS,
        "grace_seconds": licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS,
        "grace_extends_cloud_access": False,
        "upgrade_url": licensing.upgrade_url(),
    })


class _LicenseActivateReq(BaseModel):
    key: str


@router.post("/license/activate")
async def activate_license(req: _LicenseActivateReq):
    """Legacy activation moved to the hosted account portal."""
    del req
    raise HTTPException(status_code=501, detail={
        "error": "Manage Pro and Team in Engraphis Cloud.",
        "cloud_only": True,
        "upgrade_url": licensing.upgrade_url(),
    })


@router.get("/export")
async def compliance_export(namespace: Optional[str] = None):
    """GET /memory/export — raw owner data export for the legacy local store."""
    return _ok(_compute_compliance_export(namespace))


def _compute_compliance_export(namespace: Optional[str]) -> dict:
    """Full raw workspace dump; hosted signed reports remain a Cloud feature."""
    docs = mem_store.list_documents(namespace=namespace, limit=100000)
    return {"exported_at": time.time(), "namespace": namespace,
            "count": len(docs), "memories": docs}
