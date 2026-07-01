"""Vault management, file editing, folder import, memory health, bulk ops, and context preview routes."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel

from engraphis.engines import embedder, ingest as ingest_engine, recall as recall_engine, reweight
from engraphis.engines.intelligence import auto_categorize, check_conflicts
from engraphis.engines.reweight import retention_score
from engraphis.stores import get_conn, now_ts
from engraphis.stores import vaults as vault_store
from engraphis.stores import vectors as mem_store

logger = logging.getLogger("engraphis.routes.vault")
router = APIRouter(prefix="/memory", tags=["vault-management"])


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data}


# ═══ VAULT MANAGEMENT ═══════════════════════════════════════════════════════

class VaultCreateReq(BaseModel):
    namespace: str
    name: str
    description: str = ""
    color: str = "#9d7cf6"
    memory_type: str = "semantic"


class VaultUpdateReq(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    memory_type: Optional[str] = None


@router.get("/vaults")
async def list_vaults():
    return _ok(vault_store.list_vaults())


@router.post("/vaults")
async def create_vault(req: VaultCreateReq):
    if vault_store.get_vault(req.namespace):
        raise HTTPException(409, f"Vault '{req.namespace}' already exists")
    return _ok(vault_store.create_vault(
        namespace=req.namespace, name=req.name, description=req.description,
        color=req.color, memory_type=req.memory_type,
    ))


@router.put("/vaults/{namespace}")
async def update_vault(namespace: str, req: VaultUpdateReq):
    vault = vault_store.update_vault(
        namespace, name=req.name, description=req.description,
        color=req.color, memory_type=req.memory_type,
    )
    if not vault:
        raise HTTPException(404, f"Vault '{namespace}' not found")
    return _ok(vault)


@router.post("/vaults/{namespace}/activate")
async def activate_vault(namespace: str):
    if not vault_store.get_vault(namespace):
        raise HTTPException(404, f"Vault '{namespace}' not found")
    vault_store.set_active_vault(namespace)
    return _ok({"namespace": namespace, "is_active": True})


@router.delete("/vaults/{namespace}")
async def delete_vault(namespace: str, delete_memories: bool = True):
    if not vault_store.get_vault(namespace):
        raise HTTPException(404, f"Vault '{namespace}' not found")
    return _ok(vault_store.delete_vault(namespace, delete_memories=delete_memories))


@router.get("/vaults/active")
async def get_active_vault():
    vault = vault_store.get_active_vault()
    if not vault:
        vault_store.ensure_default_vault()
        vault = vault_store.get_active_vault()
    return _ok(vault)


@router.get("/vaults/{namespace}/types")
async def vault_type_breakdown(namespace: str):
    """GET /memory/vaults/{namespace}/types — memory type breakdown for a vault."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT memory_type, COUNT(*) as count FROM memories WHERE namespace=? GROUP BY memory_type",
        (namespace,),
    ).fetchall()
    return _ok({"namespace": namespace, "types": [dict(r) for r in rows]})


# ═══ FILE EDITING ═══════════════════════════════════════════════════════════

class EditMemoryReq(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[dict] = None
    memory_type: Optional[str] = None


@router.put("/documents/{document_id}")
async def edit_memory(document_id: str, req: EditMemoryReq,
                      namespace: str = Query(...)):
    """PUT /memory/documents/{id}?namespace=... — edit a memory, re-embeds on content change."""
    existing = mem_store.get_memory(namespace, document_id)
    if not existing:
        raise HTTPException(404, f"Memory '{document_id}' not found in '{namespace}'")

    vec = None
    if req.content is not None and req.content != existing["content"]:
        full_text = f"{req.title or existing['title']}\n\n{req.content}"
        vec = embedder.embed(full_text)

    updated = mem_store.update_memory_content(
        namespace, document_id,
        title=req.title, content=req.content,
        metadata=req.metadata, vector=vec,
        memory_type=req.memory_type,
    )
    return _ok(updated)


class CreateMemoryReq(BaseModel):
    title: str
    content: str
    namespace: Optional[str] = None
    document_id: Optional[str] = None
    source_type: str = "manual"
    metadata: Optional[dict] = None
    memory_type: str = "semantic"


@router.post("/files/create")
async def create_memory_file(req: CreateMemoryReq):
    """POST /memory/files/create — create a new memory file in the active or specified vault."""
    ns = req.namespace
    if not ns:
        active = vault_store.get_active_vault()
        ns = active["namespace"] if active else "default"
    doc_id = req.document_id or f"doc-{int(time.time()*1000)}"
    result = ingest_engine.ingest_document(
        namespace=ns, document_id=doc_id, title=req.title,
        content=req.content, source_type=req.source_type,
        metadata=req.metadata, memory_type=req.memory_type,
    )
    return _ok(result)


class MoveMemoryReq(BaseModel):
    from_namespace: str
    to_namespace: str
    document_id: str


@router.post("/files/move")
async def move_memory(req: MoveMemoryReq):
    """POST /memory/files/move — move a memory between vaults."""
    success = mem_store.move_memory(req.document_id, req.from_namespace, req.to_namespace)
    if not success:
        raise HTTPException(404, "Memory not found")
    return _ok({"moved": True, "document_id": req.document_id,
                "from": req.from_namespace, "to": req.to_namespace})


# ═══ FOLDER IMPORT ══════════════════════════════════════════════════════════

class FolderImportReq(BaseModel):
    path: str
    namespace: Optional[str] = None
    file_pattern: str = "*.md"
    memory_type: str = "semantic"


@router.post("/vaults/import-folder")
async def import_folder(req: FolderImportReq):
    """POST /memory/vaults/import-folder — import all .md files from a disk path."""
    folder = Path(req.path)
    if not folder.exists():
        raise HTTPException(404, f"Path not found: {req.path}")
    if not folder.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")

    ns = req.namespace
    if not ns:
        active = vault_store.get_active_vault()
        ns = active["namespace"] if active else "default"

    # Ensure vault exists
    if not vault_store.get_vault(ns):
        vault_store.create_vault(namespace=ns, name=ns, memory_type="semantic")

    import fnmatch
    files = []
    for f in folder.rglob("*"):
        if f.is_file() and fnmatch.fnmatch(f.name, req.file_pattern):
            if "node_modules" in str(f) or ".git" in str(f):
                continue
            files.append(f)

    results = {"imported": 0, "errors": 0, "skipped": 0, "files": []}
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                results["skipped"] += 1
                continue
            rel = f.relative_to(folder).as_posix()
            doc_id = rel.replace("/", "__").replace(".md", "").replace(".", "-")
            # Extract title from first H1
            import re
            title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else f.stem

            ingest_engine.ingest_document(
                namespace=ns, document_id=doc_id, title=title,
                content=content, source_type="folder_import",
                metadata={"original_path": rel, "filename": f.name},
                memory_type=req.memory_type,
            )
            results["imported"] += 1
            results["files"].append({"path": rel, "title": title, "status": "ok"})
        except Exception as e:
            results["errors"] += 1
            results["files"].append({"path": str(f), "title": "", "status": "error", "error": str(e)})

    return _ok({"namespace": ns, "folder": req.path, **results})


@router.post("/vaults/upload-folder")
async def upload_folder(
    files: list[UploadFile] = File(...),
    namespace: str = Form(...),
    memory_type: str = Form("semantic"),
):
    """POST /memory/vaults/upload-folder — upload multiple files as a folder (multipart).
    Use webkitdirectory in the frontend to send an entire folder."""
    if not vault_store.get_vault(namespace):
        vault_store.create_vault(namespace=namespace, name=namespace)

    results = {"imported": 0, "errors": 0, "files": []}
    for f in files:
        try:
            content = f.file.read().decode("utf-8", errors="replace")
            if not content.strip():
                continue
            import re
            title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else Path(f.filename).stem
            doc_id = f.filename.replace("/", "__").replace("\\", "__").replace(".md", "").replace(".", "-")

            ingest_engine.ingest_document(
                namespace=namespace, document_id=doc_id, title=title,
                content=content, source_type="folder_upload",
                metadata={"filename": f.filename},
                memory_type=memory_type,
            )
            results["imported"] += 1
            results["files"].append({"path": f.filename, "title": title, "status": "ok"})
        except Exception as e:
            results["errors"] += 1
            results["files"].append({"path": f.filename, "title": "", "status": "error", "error": str(e)})

    return _ok({"namespace": namespace, **results})


# ═══ SMART IMPORT (batch embedding + auto-categorize) ═══════════════════════

@router.post("/vaults/upload-folder-smart")
async def upload_folder_smart(
    files: list[UploadFile] = File(...),
    namespace: str = Form(...),
    memory_type: str = Form("semantic"),
    auto_categorize_flag: str = Form("false"),
):
    """POST /memory/vaults/upload-folder-smart — batch import with fast embedding.

    If auto_categorize_flag is 'true', each file is classified by the LLM
    into the correct memory type. Uses batch embedding for speed."""
    import re as _re
    if not vault_store.get_vault(namespace):
        vault_store.create_vault(namespace=namespace, name=namespace)

    do_auto = auto_categorize_flag.lower() in ("true", "1", "yes")
    results = {"imported": 0, "errors": 0, "skipped": 0, "categorized": 0, "split": 0, "files": []}

    # Phase 1: Read all files and prepare content
    file_data = []
    for f in files:
        try:
            content = f.file.read().decode("utf-8", errors="replace")
            if not content.strip():
                results["skipped"] += 1
                continue
            title_match = _re.search(r"^#\s+(.+)$", content, _re.MULTILINE)
            title = title_match.group(1).strip() if title_match else Path(f.filename).stem
            doc_id = f.filename.replace("/", "__").replace("\\", "__").replace(".md", "").replace(".", "-")
            file_data.append({"filename": f.filename, "doc_id": doc_id, "title": title, "content": content})
        except Exception as e:
            results["errors"] += 1
            results["files"].append({"path": f.filename, "title": "", "status": "error", "error": str(e)})

    # Phase 2: Batch embed all files at once (10x faster than individual)
    if file_data:
        texts = [f"{fd['title']}\n\n{fd['content']}" for fd in file_data]
        try:
            vecs = embedder.embed_batch(texts)
        except Exception:
            # Fallback: embed individually
            vecs = [embedder.embed(t) for t in texts]

    # Phase 3: Auto-categorize (if enabled) and store
    for i, fd in enumerate(file_data):
        try:
            mem_type = memory_type
            categorize_info = None

            if do_auto:
                cat = auto_categorize(fd["content"], fd["title"], memory_type)
                mem_type = cat.get("memory_type", memory_type)
                categorize_info = cat
                results["categorized"] += 1

                # If LLM says to split, create separate memories
                if cat.get("should_split") and cat.get("splits"):
                    for split in cat["splits"]:
                        split_title = split.get("title", fd["title"])
                        split_content = split.get("content", fd["content"])
                        split_type = split.get("memory_type", mem_type)
                        split_vec = embedder.embed(f"{split_title}\n\n{split_content}")
                        ingest_engine.ingest_document(
                            namespace=namespace,
                            document_id=f"{fd['doc_id']}__{split_title[:20].replace(' ', '-')}",
                            title=split_title,
                            content=split_content,
                            source_type="smart_import_split",
                            metadata={"filename": fd["filename"], "parent": fd["doc_id"]},
                            memory_type=split_type,
                            vector=split_vec,
                        )
                        results["split"] += 1
                    # Store the original too
                    ingest_engine.ingest_document(
                        namespace=namespace, document_id=fd["doc_id"], title=fd["title"],
                        content=fd["content"], source_type="smart_import",
                        metadata={"filename": fd["filename"]},
                        memory_type=mem_type, vector=vecs[i],
                    )
                    results["imported"] += 1
                    results["files"].append({"path": fd["filename"], "title": fd["title"],
                                            "status": "ok", "type": mem_type,
                                            "split": len(cat["splits"])})
                    continue

            ingest_engine.ingest_document(
                namespace=namespace, document_id=fd["doc_id"], title=fd["title"],
                content=fd["content"], source_type="smart_import",
                metadata={"filename": fd["filename"]},
                memory_type=mem_type, vector=vecs[i],
            )
            results["imported"] += 1
            results["files"].append({
                "path": fd["filename"], "title": fd["title"],
                "status": "ok", "type": mem_type,
                "categorized": categorize_info is not None,
                "confidence": categorize_info.get("confidence", 0) if categorize_info else 0,
            })
        except Exception as e:
            results["errors"] += 1
            results["files"].append({"path": fd["filename"], "title": "", "status": "error", "error": str(e)})

    return _ok({"namespace": namespace, **results})


# ═══ AUTO-CATEGORIZE EXISTING ══════════════════════════════════════════════

class AutoCategorizeReq(BaseModel):
    namespace: Optional[str] = None
    document_ids: Optional[list[str]] = None


@router.post("/auto-categorize")
async def auto_categorize_memories(req: AutoCategorizeReq):
    """POST /memory/auto-categorize — use LLM to categorize existing memories."""
    if req.document_ids and req.namespace:
        docs = [mem_store.get_memory(req.namespace, d) for d in req.document_ids]
        docs = [d for d in docs if d]
    else:
        docs = mem_store.list_documents(namespace=req.namespace, limit=10000)

    results = {"categorized": 0, "errors": 0, "details": []}
    for doc in docs:
        try:
            cat = auto_categorize(doc["content"], doc["title"], doc.get("memory_type", "semantic"))
            new_type = cat.get("memory_type", doc.get("memory_type", "semantic"))
            if new_type != doc.get("memory_type"):
                mem_store.update_memory_content(
                    doc["namespace"], doc["document_id"], memory_type=new_type,
                )
            results["categorized"] += 1
            results["details"].append({
                "document_id": doc["document_id"],
                "title": doc["title"],
                "old_type": doc.get("memory_type", "semantic"),
                "new_type": new_type,
                "confidence": cat.get("confidence", 0),
                "reason": cat.get("reason", ""),
            })
        except Exception:
            results["errors"] += 1

    return _ok(results)


# ═══ CONFLICT CHECK ═════════════════════════════════════════════════════════

class ConflictCheckReq(BaseModel):
    content: str
    namespace: str
    title: str = ""


@router.post("/conflict-check")
async def conflict_check(req: ConflictCheckReq):
    """POST /memory/conflict-check — check if content conflicts with existing memories."""
    existing = mem_store.list_documents(namespace=req.namespace, limit=10)
    result = check_conflicts(req.content, req.namespace, existing)
    return _ok(result)


# ═══ MEMORY HEALTH ══════════════════════════════════════════════════════════

@router.get("/health/duplicates")
async def find_duplicates(namespace: Optional[str] = None, threshold: float = 0.85):
    """GET /memory/health/duplicates — find near-duplicate memories by vector similarity."""
    candidates = mem_store.all_vectors(namespace=namespace)
    duplicates = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            id1, ns1, doc1, vec1, mem1 = candidates[i]
            id2, ns2, doc2, vec2, mem2 = candidates[j]
            if ns1 != ns2:
                continue
            sim = float(np.dot(vec1, vec2))
            if sim >= threshold:
                duplicates.append({
                    "namespace": ns1,
                    "memory_a": {"document_id": doc1, "title": mem1["title"], "content": mem1["content"][:200]},
                    "memory_b": {"document_id": doc2, "title": mem2["title"], "content": mem2["content"][:200]},
                    "similarity": round(sim, 4),
                })
    duplicates.sort(key=lambda x: x["similarity"], reverse=True)
    return _ok({"duplicates": duplicates, "count": len(duplicates)})


@router.get("/health/stale")
async def find_stale(namespace: Optional[str] = None, min_age_days: int = 30,
                     max_retention: float = 0.1):
    """GET /memory/health/stale — find memories with low retention and old age."""
    all_mems = mem_store.list_documents(namespace=namespace, limit=10000)
    now = now_ts()
    stale = []
    for m in all_mems:
        age_days = (now - m.get("updated_at", now)) / 86400
        ret = retention_score(m)
        if age_days >= min_age_days and ret <= max_retention:
            stale.append({
                "document_id": m["document_id"],
                "namespace": m["namespace"],
                "title": m["title"],
                "age_days": round(age_days),
                "retention": round(ret, 4),
                "stability": round(m.get("stability", 0), 2),
                "access_count": m.get("access_count", 0),
                "content_preview": m["content"][:150],
            })
    stale.sort(key=lambda x: x["retention"])
    return _ok({"stale": stale, "count": len(stale)})


@router.get("/health/overview")
async def health_overview(namespace: Optional[str] = None):
    """GET /memory/health/overview — aggregate health metrics."""
    all_mems = mem_store.list_documents(namespace=namespace, limit=10000)
    retentions = [retention_score(m) for m in all_mems]
    healthy = sum(1 for r in retentions if r > 0.5)
    decaying = sum(1 for r in retentions if 0.2 < r <= 0.5)
    critical = sum(1 for r in retentions if r <= 0.2)
    never_accessed = sum(1 for m in all_mems if m.get("access_count", 0) == 0)
    avg_stability = sum(m.get("stability", 1) for m in all_mems) / len(all_mems) if all_mems else 0

    return _ok({
        "total": len(all_mems),
        "healthy": healthy,
        "decaying": decaying,
        "critical": critical,
        "never_accessed": never_accessed,
        "avg_retention": round(sum(retentions) / len(retentions), 4) if retentions else 0,
        "avg_stability": round(avg_stability, 2),
        "health_score": round(healthy / len(all_mems), 4) if all_mems else 1.0,
    })


# ═══ BULK OPERATIONS ═══════════════════════════════════════════════════════

class BulkDeleteReq(BaseModel):
    namespace: str
    document_ids: list[str]


@router.post("/bulk/delete")
async def bulk_delete(req: BulkDeleteReq):
    """POST /memory/bulk/delete — delete multiple memories."""
    count = mem_store.bulk_delete(req.namespace, req.document_ids)
    return _ok({"deleted": count})


class BulkReembedReq(BaseModel):
    namespace: str
    document_ids: Optional[list[str]] = None


@router.post("/bulk/reembed")
async def bulk_reembed(req: BulkReembedReq):
    """POST /memory/bulk/reembed — re-embed all (or selected) memories in a vault."""
    if req.document_ids:
        docs = [mem_store.get_memory(req.namespace, d) for d in req.document_ids]
        docs = [d for d in docs if d]
    else:
        docs = mem_store.list_documents(namespace=req.namespace, limit=10000)

    count = 0
    for doc in docs:
        full_text = f"{doc['title']}\n\n{doc['content']}"
        vec = embedder.embed(full_text)
        mem_store.update_memory_content(doc["namespace"], doc["document_id"], vector=vec)
        count += 1
    return _ok({"reembedded": count})


@router.post("/bulk/decay")
async def force_decay(namespace: Optional[str] = None):
    """POST /memory/bulk/decay — force an Ebbinghaus decay pass."""
    from engraphis.config import settings
    touched = reweight.decay_pass(namespace)
    return _ok({"decayed": touched, "halflife_days": settings.decay_halflife_days})


# ═══ CONTEXT PREVIEW ════════════════════════════════════════════════════════

class ContextPreviewReq(BaseModel):
    query: str
    namespace: Optional[str] = None
    max_chunks: int = 10


@router.post("/context-preview")
async def context_preview(req: ContextPreviewReq):
    """POST /memory/context-preview — preview exactly what the LLM will see for a query."""
    result = recall_engine.recall(
        namespace=req.namespace, prompt=req.query,
        num_chunks=req.max_chunks, reinforce=False,
    )
    chunks = result.get("chunks", [])
    context_text = result.get("llmContextMessage", "")

    # Estimate token count (~4 chars per token)
    token_est = len(context_text) // 4

    return _ok({
        "query": req.query,
        "context_text": context_text,
        "chunks": chunks,
        "chunk_count": len(chunks),
        "estimated_tokens": token_est,
        "context_length": len(context_text),
    })


# ═══ EXPORT ════════════════════════════════════════════════════════════════

@router.get("/vaults/{namespace}/export")
async def export_vault(namespace: str):
    """GET /memory/vaults/{namespace}/export — export all memories in a vault as JSON."""
    docs = mem_store.list_documents(namespace=namespace, limit=10000)
    export_data = {
        "namespace": namespace,
        "exported_at": now_ts(),
        "count": len(docs),
        "memories": docs,
    }
    return _ok(export_data)
