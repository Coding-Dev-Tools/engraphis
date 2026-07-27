"""Vault management, file editing, folder import, memory health, bulk ops, and context preview routes."""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from engraphis.engines import embedder, ingest as ingest_engine, recall as recall_engine, reweight
from engraphis.service import MAX_IMPORT_FILES, MAX_IMPORT_RESOURCE_BYTES, MAX_IMPORT_TOTAL_BYTES
from engraphis.engines.intelligence import auto_categorize, check_conflicts
from engraphis.engines.reweight import retention_score
from engraphis.stores import blob_to_vector, get_conn, now_ts
from engraphis.stores import vaults as vault_store
from engraphis.stores import vectors as mem_store

logger = logging.getLogger("engraphis.routes.vault")
# Multipart boundaries and per-part headers count toward the HTTP request size even
# though they are not imported content. Keep the transport ceiling finite while allowing
# the documented content limit plus conservative multipart overhead.
VAULT_UPLOAD_REQUEST_BYTES = (
    MAX_IMPORT_TOTAL_BYTES + MAX_IMPORT_FILES * 16_384 + 1024 * 1024
)
_UPLOAD_FORM_FIELDS = 8
_DUPLICATE_CANDIDATE_LIMIT = 500
_DUPLICATE_RESULT_LIMIT = 200
_DUPLICATE_BLOCK_SIZE = 256


class _BoundedUploadRoute(APIRoute):
    """Parse vault uploads with their strict multipart limits before FastAPI binds files."""

    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def bounded_route_handler(request: Request):
            # FastAPI normally resolves ``UploadFile`` parameters before dependencies.
            # Parsing here runs first and caches the bounded FormData on this same request.
            await _bounded_upload_form(request)
            return await route_handler(request)

        return bounded_route_handler


class _VaultRouter(APIRouter):
    """Install the bounded parser only on the two multipart folder-import routes."""

    _bounded_upload_paths = {
        "/vaults/upload-folder",
        "/vaults/upload-folder-smart",
    }

    def add_api_route(self, path: str, endpoint, **kwargs):
        if path in self._bounded_upload_paths:
            kwargs["route_class_override"] = _BoundedUploadRoute
        return super().add_api_route(path, endpoint, **kwargs)


router = _VaultRouter(prefix="/memory", tags=["vault-management"])


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data}


async def _bounded_upload_form(request: Request) -> None:
    """Parse multipart once with a strict file-count ceiling.

    FastAPI otherwise parses ``UploadFile`` dependencies with Starlette's default
    1,000-file ceiling before the route can inspect ``len(files)``. The app-level
    middleware separately bounds bytes before this parser is allowed to spool them.
    """
    try:
        await request.form(
            max_files=MAX_IMPORT_FILES,
            max_fields=_UPLOAD_FORM_FIELDS,
        )
    except StarletteHTTPException as exc:
        detail = str(exc.detail)
        if exc.status_code == 400 and detail.lower().startswith("too many files"):
            raise HTTPException(
                status_code=413,
                detail={"error": f"too many files (max {MAX_IMPORT_FILES})"},
            ) from exc
        raise


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
    # Guard against path traversal: only allow import from directories that are
    # explicitly configured or under the user's home directory.
    import os
    home = os.path.realpath(str(Path.home().expanduser()))
    allowed_roots = [home]
    env_roots = os.environ.get("ENGRAPHIS_IMPORT_ROOTS", "")
    if env_roots:
        allowed_roots.extend(
            os.path.realpath(os.path.expanduser(root))
            for root in env_roots.split(os.pathsep)
            if root
        )
    real_path = os.path.realpath(os.path.expanduser(req.path))
    comparable_path = os.path.normcase(real_path)
    safe_path = None
    for root in allowed_roots:
        comparable_root = os.path.normcase(root)
        if comparable_path == comparable_root:
            safe_path = comparable_root
            break
        root_prefix = comparable_root.rstrip(os.sep) + os.sep
        if comparable_path.startswith(root_prefix):
            safe_path = comparable_path
            break
    if safe_path is None:
        raise HTTPException(403, "Import path must be under an allowed root (home directory or ENGRAPHIS_IMPORT_ROOTS)")
    folder = Path(safe_path)
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
        if not f.is_file() or not fnmatch.fnmatch(f.name, req.file_pattern):
            continue
        try:
            # Read only the resolved, allowlisted file.  In particular, do not let a
            # symlink inside an import root redirect this legacy route outside it.
            real = f.resolve(strict=True)
            rel = real.relative_to(folder)
        except (OSError, ValueError):
            continue
        if any(part in {"node_modules", ".git"} for part in rel.parts[:-1]):
            continue
        files.append((real, rel))

    results = {"imported": 0, "errors": 0, "skipped": 0, "files": []}
    for f, rel_path in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                results["skipped"] += 1
                continue
            rel = rel_path.as_posix()
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
        except Exception as exc:
            logger.warning("Folder import file failed (%s)", type(exc).__name__)
            results["errors"] += 1
            results["files"].append({"path": rel_path.as_posix(), "title": "", "status": "error"})

    return _ok({"namespace": ns, "folder": req.path, **results})


@router.post("/vaults/upload-folder")
async def upload_folder(
    files: list[UploadFile] = File(...),
    namespace: str = Form(...),
    memory_type: str = Form("semantic"),
):
    """POST /memory/vaults/upload-folder — upload multiple files as a folder (multipart).
    Use webkitdirectory in the frontend to send an entire folder."""
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail={"error": f"too many files (max {MAX_IMPORT_FILES})"})
    if not vault_store.get_vault(namespace):
        vault_store.create_vault(namespace=namespace, name=namespace)

    results = {"imported": 0, "errors": 0, "files": []}
    total_bytes = 0
    for f in files:
        try:
            raw = f.file.read(MAX_IMPORT_RESOURCE_BYTES + 1)
            if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
                results["errors"] += 1
                results["files"].append({"path": f.filename, "title": "", "status": "error", "error": "file too large"})
                continue
            total_bytes += len(raw)
            if total_bytes > MAX_IMPORT_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail={"error": f"upload batch exceeds {MAX_IMPORT_TOTAL_BYTES} bytes"})
            content = raw.decode("utf-8", errors="replace")
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
        except HTTPException:
            raise
        except Exception as exc:
            results["errors"] += 1
            logger.warning("Folder upload file failed (%s)", type(exc).__name__)
            results["files"].append({"path": f.filename, "title": "", "status": "error", "error": "processing failed"})

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
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail={"error": f"too many files (max {MAX_IMPORT_FILES})"})
    if not vault_store.get_vault(namespace):
        vault_store.create_vault(namespace=namespace, name=namespace)

    do_auto = auto_categorize_flag.lower() in ("true", "1", "yes")
    results = {"imported": 0, "errors": 0, "skipped": 0, "categorized": 0, "split": 0, "files": []}

    # Phase 1: Read all files and prepare content
    file_data = []
    total_bytes = 0
    for f in files:
        try:
            raw = f.file.read(MAX_IMPORT_RESOURCE_BYTES + 1)
            if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
                results["errors"] += 1
                results["files"].append({"path": f.filename, "title": "", "status": "error", "error": "file too large"})
                continue
            total_bytes += len(raw)
            if total_bytes > MAX_IMPORT_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail={"error": f"upload batch exceeds {MAX_IMPORT_TOTAL_BYTES} bytes"})
            content = raw.decode("utf-8", errors="replace")
            if not content.strip():
                results["skipped"] += 1
                continue
            title_match = _re.search(r"^#\s+(.+)$", content, _re.MULTILINE)
            title = title_match.group(1).strip() if title_match else Path(f.filename).stem
            doc_id = f.filename.replace("/", "__").replace("\\", "__").replace(".md", "").replace(".", "-")
            file_data.append({"filename": f.filename, "doc_id": doc_id, "title": title, "content": content})
        except HTTPException:
            raise
        except Exception as exc:
            results["errors"] += 1
            logger.warning("Smart import file read failed (%s)", type(exc).__name__)
            results["files"].append({"path": f.filename, "title": "", "status": "error", "error": "processing failed"})

    # Phase 2: Batch embed all files at once (10x faster than individual)
    if file_data:
        texts = [f"{fd['title']}\n\n{fd['content']}" for fd in file_data]
        try:
            vecs = embedder.embed_batch(texts)
        except Exception as exc:
            logger.warning("Batch embedding failed (%s), falling back to individual", type(exc).__name__)
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
        except Exception as exc:
            results["errors"] += 1
            logger.warning("Smart import ingest failed (%s)", type(exc).__name__)
            results["files"].append({"path": fd["filename"], "title": "", "status": "error", "error": "processing failed"})

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
        except Exception as exc:
            logger.warning("Auto-categorize failed (%s)", type(exc).__name__)
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

def _duplicate_pairs(
    candidates: list[tuple[str, str, np.ndarray, dict[str, str]]],
    threshold: float,
) -> tuple[list[dict[str, Any]], int]:
    """Find the strongest pairs with bounded block memory and response cardinality."""
    groups: dict[tuple[str, int], list[tuple[str, np.ndarray, dict[str, str]]]] = (
        defaultdict(list)
    )
    for namespace, document_id, vector, memory in candidates:
        # A stale vector from an old embedding dimension must not break all health data.
        groups[(namespace, int(vector.size))].append((document_id, vector, memory))

    strongest: list[tuple[float, int, int, int, list[tuple[str, np.ndarray, dict[str, str]]], str]] = []
    match_count = 0
    sequence = 0
    for (namespace, _dimension), memories in groups.items():
        if len(memories) < 2:
            continue
        vectors = np.stack([item[1] for item in memories])
        for start in range(0, len(memories), _DUPLICATE_BLOCK_SIZE):
            stop = min(start + _DUPLICATE_BLOCK_SIZE, len(memories))
            similarities = vectors[start:stop] @ vectors.T
            for local_index, absolute_index in enumerate(range(start, stop)):
                similarities[local_index, :absolute_index + 1] = -np.inf

            flat = similarities.ravel()
            matching = np.flatnonzero(flat >= threshold)
            match_count += int(matching.size)
            if matching.size > _DUPLICATE_RESULT_LIMIT:
                relative = np.argpartition(
                    flat[matching],
                    -_DUPLICATE_RESULT_LIMIT,
                )[-_DUPLICATE_RESULT_LIMIT:]
                matching = matching[relative]

            for flat_index in matching:
                local_index, right_index = divmod(int(flat_index), len(memories))
                left_index = start + local_index
                similarity = float(similarities[local_index, right_index])
                entry = (
                    similarity,
                    sequence,
                    left_index,
                    right_index,
                    memories,
                    namespace,
                )
                sequence += 1
                if len(strongest) < _DUPLICATE_RESULT_LIMIT:
                    heapq.heappush(strongest, entry)
                elif similarity > strongest[0][0]:
                    heapq.heapreplace(strongest, entry)

    duplicates = []
    for similarity, _, left_index, right_index, memories, namespace in sorted(
        strongest, key=lambda item: (-item[0], item[1])
    ):
        left_doc, _, left_mem = memories[left_index]
        right_doc, _, right_mem = memories[right_index]
        duplicates.append({
            "namespace": namespace,
            "memory_a": {
                "document_id": left_doc,
                "title": left_mem["title"],
                "content": left_mem["content"][:200],
            },
            "memory_b": {
                "document_id": right_doc,
                "title": right_mem["title"],
                "content": right_mem["content"][:200],
            },
            "similarity": round(similarity, 4),
        })
    return duplicates, match_count


def _duplicate_candidate_query(namespace: Optional[str]) -> tuple[str, list[Any]]:
    """Build the bounded duplicate-candidate query without SQLite temporary sorting.

    A namespaced scan is ordered by newest update through ``idx_mem_updated``. A global
    scan instead uses descending rowid/``id`` (newest insertion), because there is no
    global ``updated_at`` index and combining the two would force a temp B-tree.
    """
    sql = (
        "SELECT namespace, document_id, vector, title, content "
        "FROM memories WHERE vector IS NOT NULL"
    )
    params: list[Any] = []
    if namespace:
        sql += " AND namespace=?"
        params.append(namespace)
        sql += " ORDER BY updated_at DESC"
    else:
        sql += " ORDER BY id DESC"
    return sql, params


@router.get("/health/duplicates")
async def find_duplicates(
    namespace: Optional[str] = None,
    threshold: float = Query(0.85, ge=-1.0, le=1.0),
):
    """Find a bounded set of strongest near-duplicates without blocking the event loop."""
    sql, params = _duplicate_candidate_query(namespace)
    sql += " LIMIT ?"
    params.append(_DUPLICATE_CANDIDATE_LIMIT + 1)
    rows = get_conn().execute(sql, params).fetchall()
    candidate_truncated = len(rows) > _DUPLICATE_CANDIDATE_LIMIT
    candidates = [
        (
            row["namespace"],
            row["document_id"],
            blob_to_vector(row["vector"]),
            {"title": row["title"], "content": row["content"]},
        )
        for row in rows[:_DUPLICATE_CANDIDATE_LIMIT]
    ]
    duplicates, match_count = await asyncio.to_thread(
        _duplicate_pairs, candidates, threshold
    )
    return _ok({
        "duplicates": duplicates,
        "count": len(duplicates),
        "matches_considered": match_count,
        "candidate_count": len(candidates),
        "candidate_limit": _DUPLICATE_CANDIDATE_LIMIT,
        "result_limit": _DUPLICATE_RESULT_LIMIT,
        "truncated": (
            candidate_truncated or match_count > _DUPLICATE_RESULT_LIMIT
        ),
    })


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
