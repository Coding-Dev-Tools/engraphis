"""All Engraphis-compatible API routes, mounted under /memory.

Every route returns {"data": ...} to match the upstream SDK contract
(the Python SDK does `payload["data"]` on every response).
"""
from __future__ import annotations

import logging
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

logger = logging.getLogger("engraphis.routes")
router = APIRouter(prefix="/memory", tags=["memory"])


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data}


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
    result = ingest_engine.ingest_document(
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
    result = recall_engine.recall(
        namespace=req.namespace,
        prompt=prompt,
        num_chunks=req.maxChunks or req.num_chunks or 10,
        document_ids=doc_ids,
    )
    return _ok(result)


@router.post("/admin/delete")
async def delete_memory(req: DeleteMemoryRequest):
    """POST /memory/admin/delete — delete a namespace (must confirm with delete_all=True)."""
    confirm = req.delete_all or (req.deleteAll or False)
    if not confirm:
        raise HTTPException(400, "Set delete_all=True to confirm namespace deletion")
    count = mem_store.delete_namespace(req.namespace)
    return _ok({"deleted": count, "nodesDeleted": count})


# ── Documents routes ─────────────────────────────────────────────────────────

@router.post("/documents")
async def insert_document(req: InsertDocumentRequest):
    """POST /memory/documents — insert a single document."""
    doc_id = _norm_doc_id(req)
    result = ingest_engine.ingest_document(
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
    result = ingest_engine.ingest_batch(items)
    return _ok(result)


@router.get("/documents")
async def list_documents(namespace: Optional[str] = None, limit: Optional[int] = None,
                         offset: Optional[int] = None):
    """GET /memory/documents — list documents."""
    docs = mem_store.list_documents(namespace=namespace, limit=limit, offset=offset)
    return _ok({"documents": docs, "count": len(docs)})


@router.get("/documents/{document_id}")
async def get_document(document_id: str, namespace: Optional[str] = None):
    """GET /memory/documents/{documentId} — get a single document."""
    doc = mem_store.get_memory(namespace or "_global", document_id)
    if not doc:
        raise HTTPException(404, f"Document {document_id} not found")
    return _ok(doc)


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, namespace: str = Query(...)):
    """DELETE /memory/documents/{documentId} — delete a single document."""
    count = mem_store.delete_memory_document(document_id, namespace)
    return _ok({"deleted": count, "documentId": document_id})


# ── Queries / conversations (mirrored endpoints) ────────────────────────────

@router.post("/queries")
async def query_memory_context(req: QueryContextRequest):
    """POST /memory/queries — query memory context with optional LLM."""
    doc_ids = req.documentIds or req.document_ids
    result = recall_engine.recall(
        namespace=req.namespace,
        prompt=req.query,
        num_chunks=req.maxChunks or 10,
        document_ids=doc_ids,
    )
    if req.recallOnly:
        return _ok(result)
    if req.llmQuery or req.query:
        try:
            with LLMClient() as llm:
                answer = llm.chat_with_context(
                    user_prompt=req.llmQuery or req.query,
                    context=result.get("llmContextMessage", ""),
                )
            result["answer"] = answer
        except Exception as e:
            result["llm_error"] = str(e)
    return _ok(result)


@router.post("/conversations")
async def chat_memory_context(req: ChatRequest):
    """POST /memory/conversations — chat with memory context."""
    user_msg = next((m for m in reversed(req.messages) if m.get("role") == "user"), None)
    if not user_msg:
        raise HTTPException(400, "At least one user message is required")
    ctx = recall_engine.recall(namespace=None, prompt=user_msg["content"], num_chunks=10)
    try:
        with LLMClient() as llm:
            answer = llm.chat_with_context(
                user_prompt=user_msg["content"],
                context=ctx.get("llmContextMessage", ""),
                temperature=req.temperature,
                max_tokens=req.maxTokens or req.max_tokens,
            )
    except Exception as e:
        raise HTTPException(500, f"LLM error: {e}")
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
    for name in names:
        ledger_store.record_interaction(
            namespace=req.namespace,
            entity_name=name,
            interaction_level=level,
            description=req.description,
            timestamp=req.timestamp,
        )
    return _ok({"recorded": len(names), "namespace": req.namespace, "level": level})


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
    namespace = req.namespace or "hermes"
    mem = mem_store.get_memory(namespace, req.documentId)
    if not mem:
        raise HTTPException(404, f"Document {req.documentId} not found in namespace {namespace}")
    reweight.reinforce(mem["id"])
    return _ok({"reinforced": True, "documentId": req.documentId, "namespace": namespace})


# ── Thoughts / recall ────────────────────────────────────────────────────────

@router.post("/memories/thoughts")
async def recall_thoughts(req: ThoughtRequest):
    """POST /memory/memories/thoughts — generate reflective thoughts."""
    result = thoughts_engine.synthesize_thoughts(
        namespace=req.namespace,
        max_chunks=req.maxChunks or req.max_chunks or 10,
        temperature=req.temperature,
        randomness_seed=req.randomnessSeed or req.randomness_seed,
        persist=req.persist if req.persist is not None else True,
        thought_prompt=req.thoughtPrompt or req.thought_prompt,
    )
    return _ok(result)


@router.post("/memories/recall")
async def recall_memories(req: RecallMemoriesRequest):
    """POST /memory/memories/recall — recall from Ebbinghaus bank by retention."""
    result = recall_engine.recall_by_retention(
        namespace=req.namespace,
        top_k=int(req.topK or req.top_k or 10),
        min_retention=req.minRetention or req.min_retention or 0.0,
        as_of=req.asOf or req.as_of,
    )
    return _ok(result)


@router.post("/memories/context")
async def memories_context(namespace: Optional[str] = None, maxChunks: Optional[int] = 10):
    """POST /memory/memories/context — recall context."""
    result = recall_engine.recall_master(namespace=namespace or "_global", max_chunks=maxChunks or 10)
    return _ok(result)


@router.post("/recall")
async def recall_master(req: RecallMasterRequest):
    """POST /memory/recall — recall from master node (highest retention)."""
    result = recall_engine.recall_master(
        namespace=req.namespace,
        max_chunks=req.maxChunks or req.max_chunks or 10,
    )
    return _ok(result)


@router.post("/chat")
async def chat_memory(req: ChatRequest):
    """POST /memory/chat — chat with memory."""
    return await chat_memory_context(req)


# ── Admin / graph ────────────────────────────────────────────────────────────

@router.get("/admin/graph-snapshot")
async def graph_snapshot(namespace: Optional[str] = None, mode: Optional[str] = None,
                         limit: int = 200, seed_limit: int = 10):
    """GET /memory/admin/graph-snapshot — entity/relation graph snapshot."""
    snap = graph_store.graph_snapshot(namespace=namespace, limit=limit, seed_limit=seed_limit)
    return _ok(snap)


# ── Ingestion jobs ───────────────────────────────────────────────────────────

@router.get("/ingestion/jobs/{job_id}")
async def get_ingestion_job(job_id: str):
    """GET /memory/ingestion/jobs/{jobId} — get job status."""
    job = ledger_store.get_job(job_id)
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
async def search_documents(q: str = Query(...), namespace: Optional[str] = None,
                           limit: int = 50):
    """GET /memory/search — full-text search across document content/titles."""
    from engraphis.stores import get_conn
    conn = get_conn()
    pattern = f"%{q}%"
    if namespace:
        rows = conn.execute(
            "SELECT * FROM memories WHERE namespace=? AND (content LIKE ? OR title LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (namespace, pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE content LIKE ? OR title LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
    from engraphis.stores.vectors import _row_to_mem
    results = [_row_to_mem(r) for r in rows]
    return _ok({"results": results, "count": len(results), "query": q})


@router.get("/timeline")
async def get_timeline(namespace: Optional[str] = None, limit: int = 100):
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
        d["payload"] = json.loads(d.get("payload") or "{}")
        events.append(d)
    return _ok({"events": events, "count": len(events)})


@router.get("/thoughts")
async def list_thoughts(namespace: Optional[str] = None, limit: int = 50):
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
        d["source_memory_ids"] = json.loads(d.get("source_memory_ids") or "[]")
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
        "llm_base_url": settings.llm_base_url or "(provider default)",
        "embed_model": settings.embed_model,
        "loop_interval": settings.loop_interval,
        "decay_halflife_days": settings.decay_halflife_days,
        "host": settings.host,
        "port": settings.port,
        "base_url": settings.base_url,
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
    from collections import defaultdict
    from engraphis.stores import get_conn
    from engraphis.engines.reweight import retention_score
    conn = get_conn()
    now = time.time()

    # ── Memory creation timeline (grouped by day, last 30 days) ──────────────
    rows = conn.execute(
        "SELECT created_at FROM memories WHERE created_at > ? ORDER BY created_at",
        (now - 30 * 86400,),
    ).fetchall()
    timeline = defaultdict(int)
    for r in rows:
        day = int(r["created_at"] // 86400)
        timeline[day] += 1
    timeline_data = [
        {"day": day, "count": timeline.get(day, 0),
         "ts": day * 86400,
         "label": time.strftime("%m/%d", time.gmtime(day * 86400))}
        for day in range(int((now - 30 * 86400) // 86400), int(now // 86400) + 1)
    ]

    # ── Retention distribution histogram (10 buckets) ────────────────────────
    all_mems = mem_store.list_documents(limit=10000)
    retentions = [retention_score(m) for m in all_mems]
    buckets = [0] * 10
    for r in retentions:
        idx = min(int(r * 10), 9)
        buckets[idx] += 1
    retention_hist = [
        {"bucket": f"{i*10}-{(i+1)*10}%", "count": buckets[i], "range": [i*10, (i+1)*10]}
        for i in range(10)
    ]

    # ── Namespace distribution with avg retention ────────────────────────────
    ns_rows = conn.execute(
        "SELECT namespace, COUNT(*) as count, AVG(stability) as avg_stability, "
        "AVG(access_count) as avg_access FROM memories GROUP BY namespace ORDER BY count DESC"
    ).fetchall()
    ns_dist = []
    for r in ns_rows:
        ns_mems = [m for m in all_mems if m["namespace"] == r["namespace"]]
        ns_ret = sum(retention_score(m) for m in ns_mems) / len(ns_mems) if ns_mems else 0
        ns_dist.append({
            "namespace": r["namespace"],
            "count": r["count"],
            "avg_retention": round(ns_ret, 4),
            "avg_stability": round(r["avg_stability"] or 0, 2),
            "avg_access": round(r["avg_access"] or 0, 1),
        })

    # ── Top entities by connection degree ────────────────────────────────────
    entity_rows = conn.execute(
        """SELECT e.name, e.namespace, e.entity_type,
                  (SELECT COUNT(*) FROM edges ed WHERE ed.namespace=e.namespace
                   AND (ed.source_entity=e.name OR ed.target_entity=e.name)) as degree
           FROM entities e ORDER BY degree DESC LIMIT 20"""
    ).fetchall()
    top_entities = [dict(r) for r in entity_rows if r["degree"] > 0]

    # ── Interaction activity timeline (last 30 days) ─────────────────────────
    int_rows = conn.execute(
        "SELECT timestamp, interaction_level FROM interactions WHERE timestamp > ?",
        (now - 30 * 86400,),
    ).fetchall()
    int_timeline = defaultdict(lambda: defaultdict(int))
    for r in int_rows:
        day = int(r["timestamp"] // 86400)
        level = r["interaction_level"] or "unknown"
        int_timeline[day][level] += 1
    int_data = [
        {"day": day, "ts": day * 86400,
         "label": time.strftime("%m/%d", time.gmtime(day * 86400)),
         "levels": dict(int_timeline.get(day, {}))}
        for day in range(int((now - 30 * 86400) // 86400), int(now // 86400) + 1)
    ]

    # ── Access frequency distribution ────────────────────────────────────────
    access_rows = conn.execute(
        "SELECT access_count, COUNT(*) as cnt FROM memories GROUP BY access_count ORDER BY access_count"
    ).fetchall()
    access_dist = [{"access_count": r["access_count"], "count": r["cnt"]} for r in access_rows]

    # ── Thought generation timeline ──────────────────────────────────────────
    thought_rows = conn.execute(
        "SELECT created_at, namespace FROM thoughts WHERE created_at > ? ORDER BY created_at",
        (now - 30 * 86400,),
    ).fetchall()
    thought_timeline = defaultdict(int)
    for r in thought_rows:
        day = int(r["created_at"] // 86400)
        thought_timeline[day] += 1
    thought_data = [
        {"day": day, "count": thought_timeline.get(day, 0),
         "ts": day * 86400,
         "label": time.strftime("%m/%d", time.gmtime(day * 86400))}
        for day in range(int((now - 30 * 86400) // 86400), int(now // 86400) + 1)
    ]

    return _ok({
        "timeline": timeline_data,
        "retention_histogram": retention_hist,
        "namespace_distribution": ns_dist,
        "top_entities": top_entities,
        "interaction_timeline": int_data,
        "access_distribution": access_dist,
        "thought_timeline": thought_data,
        "total_memories": len(all_mems),
        "avg_retention": round(sum(retentions) / len(retentions), 4) if retentions else 0,
    })
