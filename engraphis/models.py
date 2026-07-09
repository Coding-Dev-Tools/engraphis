"""Pydantic request/response models mirroring the Engraphis SDK contract."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

# ── v1 input hardening: mirror engraphis/service.py's write-path guards so the REST API is
# no longer the unvalidated path (SECURITY.md). Strips control chars (defangs hidden-instruction
# / terminal-escape payloads) and caps length on stored text fields, via pydantic AfterValidator.
import re as _re
from typing import Annotated
from pydantic import AfterValidator

MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 1_000
MAX_NAME_CHARS = 200
_CONTROL_RE = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize(value, *, max_chars, field):
    if not isinstance(value, str):
        return value
    cleaned = _CONTROL_RE.sub("", value)
    if len(cleaned) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters (got {len(cleaned)})")
    return cleaned


def _mk(max_chars, field):
    return lambda v: _sanitize(v, max_chars=max_chars, field=field)


Content = Annotated[str, AfterValidator(_mk(MAX_CONTENT_CHARS, "content"))]
OptContent = Annotated[Optional[str], AfterValidator(_mk(MAX_CONTENT_CHARS, "content"))]
Title = Annotated[str, AfterValidator(_mk(MAX_TITLE_CHARS, "title"))]
Name = Annotated[str, AfterValidator(_mk(MAX_NAME_CHARS, "name"))]
OptName = Annotated[Optional[str], AfterValidator(_mk(MAX_NAME_CHARS, "name"))]


class MemoryItem(BaseModel):
    key: Name
    content: Content
    namespace: Name
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class InsertMemoryRequest(BaseModel):
    item: Optional[MemoryItem] = None
    items: Optional[list[MemoryItem]] = None
    key: OptName = None
    content: OptContent = None
    namespace: OptName = None
    metadata: Optional[dict[str, Any]] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    memory_type: Optional[str] = None
    memoryType: Optional[str] = None


class QueryMemoryRequest(BaseModel):
    query: Optional[str] = None
    prompt: Optional[str] = None
    namespace: OptName = None
    maxChunks: Optional[int] = 10
    num_chunks: Optional[int] = 10
    documentIds: Optional[list[str]] = None
    keys: Optional[list[str]] = None
    key: OptName = None


class DeleteMemoryRequest(BaseModel):
    namespace: Name
    delete_all: bool = False
    deleteAll: Optional[bool] = None


class DocumentItem(BaseModel):
    title: Title
    content: Content
    namespace: Name
    document_id: Optional[str] = None
    documentId: Optional[str] = None
    source_type: Optional[str] = None
    sourceType: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    priority: Optional[str] = None
    created_at: Optional[float] = None
    createdAt: Optional[float] = None
    updated_at: Optional[float] = None
    updatedAt: Optional[float] = None


class InsertDocumentRequest(DocumentItem):
    pass


class BatchDocumentsRequest(BaseModel):
    items: list[DocumentItem]


class QueryContextRequest(BaseModel):
    query: str
    namespace: OptName = None
    includeReferences: Optional[bool] = None
    maxChunks: Optional[int] = None
    document_ids: Optional[list[str]] = None
    documentIds: Optional[list[str]] = None
    recallOnly: Optional[bool] = None
    llmQuery: Optional[str] = None


class ChatRequest(BaseModel):
    messages: list[dict[str, str]]
    temperature: Optional[float] = None
    maxTokens: Optional[int] = None
    max_tokens: Optional[int] = None


class InteractionRequest(BaseModel):
    namespace: Name
    entityNames: list[str]
    entity_names: Optional[list[str]] = None
    description: Optional[str] = None
    interactionLevel: Optional[str] = None
    interaction_level: Optional[str] = None
    interactionLevels: Optional[list[str]] = None
    interaction_levels: Optional[list[str]] = None
    timestamp: Optional[float] = None


class ReinforceRequest(BaseModel):
    documentId: str
    namespace: OptName = None


class PruneRequest(BaseModel):
    """Prune decayed memories below a retention threshold from one namespace."""
    namespace: Name
    minRetention: Optional[float] = 0.05
    min_retention: Optional[float] = None
    dryRun: Optional[bool] = False
    dry_run: Optional[bool] = None
    keepPinned: Optional[bool] = True
    maxDelete: Optional[int] = 500


class ThoughtRequest(BaseModel):
    namespace: OptName = None
    maxChunks: Optional[int] = 10
    max_chunks: Optional[int] = 10
    temperature: Optional[float] = 0.3
    randomnessSeed: Optional[int] = None
    randomness_seed: Optional[int] = None
    persist: Optional[bool] = True
    enablePredictionCheck: Optional[bool] = None
    thoughtPrompt: Optional[str] = None
    thought_prompt: Optional[str] = None


class RecallMemoriesRequest(BaseModel):
    namespace: OptName = None
    topK: Optional[int] = 10
    top_k: Optional[int] = 10
    minRetention: Optional[float] = 0.0
    min_retention: Optional[float] = 0.0
    asOf: Optional[float] = None
    as_of: Optional[float] = None


class RecallMasterRequest(BaseModel):
    namespace: Name
    maxChunks: Optional[int] = 10
    max_chunks: Optional[int] = 10


class DataResponse(BaseModel):
    data: Any
