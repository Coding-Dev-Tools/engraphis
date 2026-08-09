"""Pydantic request/response models mirroring the Engraphis SDK contract."""
from __future__ import annotations

import json
import math
import re as _re
from typing import Annotated, Any, Optional

from pydantic import AfterValidator, BaseModel, Field

# v1 input hardening mirrors the write-path guards in ``engraphis.service``.
# Request models reject resource amplification before an embedder, SQLite, or LLM sees it.
MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 1_000
MAX_NAME_CHARS = 200
MAX_METADATA_BYTES = 100_000
MAX_BATCH_ITEMS = 1_000
MAX_NAME_LIST_ITEMS = 1_000
MAX_CHAT_MESSAGES = 100
_CONTROL_RE = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize(value: Any, *, max_chars: int, field: str) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = _CONTROL_RE.sub("", value)
    if len(cleaned) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    return cleaned


def _mk(max_chars: int, field: str):
    return lambda value: _sanitize(value, max_chars=max_chars, field=field)


def _validate_name(value: str) -> str:
    value = _sanitize(value, max_chars=MAX_NAME_CHARS, field="name")
    if not value.strip():
        raise ValueError("name must be non-empty")
    return value


def _validate_metadata(value: Any) -> Any:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("metadata must be JSON-serializable") from None
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
    return value


def _validate_timestamp(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("timestamp must be a non-negative finite number")
    return value


def _validate_name_list(values: Any) -> Any:
    if values is None:
        return None
    if len(values) > MAX_NAME_LIST_ITEMS:
        raise ValueError(f"name list exceeds {MAX_NAME_LIST_ITEMS} entries")
    return [_validate_name(value) for value in values]


def _validate_chat_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if not messages or len(messages) > MAX_CHAT_MESSAGES:
        raise ValueError(f"messages must contain 1 to {MAX_CHAT_MESSAGES} entries")
    cleaned = []
    for message in messages:
        role = _validate_name(message.get("role", ""))
        if role not in {"system", "user", "assistant"}:
            raise ValueError("message role must be system, user, or assistant")
        content = _sanitize(
            message.get("content", ""),
            max_chars=MAX_CONTENT_CHARS,
            field="message content",
        )
        cleaned.append({"role": role, "content": content})
    return cleaned


Content = Annotated[str, AfterValidator(_mk(MAX_CONTENT_CHARS, "content"))]
OptContent = Annotated[
    Optional[str], AfterValidator(_mk(MAX_CONTENT_CHARS, "content"))
]
Title = Annotated[str, AfterValidator(_mk(MAX_TITLE_CHARS, "title"))]
OptTitle = Annotated[
    Optional[str], AfterValidator(_mk(MAX_TITLE_CHARS, "title"))
]
Name = Annotated[str, AfterValidator(_validate_name)]
OptName = Annotated[Optional[str], AfterValidator(
    lambda value: None if value is None else _validate_name(value)
)]
Metadata = Annotated[dict[str, Any], AfterValidator(_validate_metadata)]
OptMetadata = Annotated[Optional[dict[str, Any]], AfterValidator(_validate_metadata)]
Timestamp = Annotated[Optional[float], AfterValidator(_validate_timestamp)]
NameList = Annotated[Optional[list[str]], AfterValidator(_validate_name_list)]
RequiredNameList = Annotated[list[str], AfterValidator(_validate_name_list)]
ChatMessages = Annotated[
    list[dict[str, str]], AfterValidator(_validate_chat_messages)
]


class MemoryItem(BaseModel):
    key: Name
    content: Content
    namespace: Name
    metadata: Metadata = Field(default_factory=dict)
    created_at: Timestamp = None
    updated_at: Timestamp = None


class InsertMemoryRequest(BaseModel):
    item: Optional[MemoryItem] = None
    items: Optional[
        Annotated[list[MemoryItem], Field(max_length=MAX_BATCH_ITEMS)]
    ] = None
    key: OptName = None
    content: OptContent = None
    namespace: OptName = None
    metadata: OptMetadata = None
    created_at: Timestamp = None
    updated_at: Timestamp = None
    memory_type: OptName = None
    memoryType: OptName = None


class QueryMemoryRequest(BaseModel):
    query: OptContent = None
    prompt: OptContent = None
    namespace: OptName = None
    maxChunks: Optional[int] = Field(default=10, ge=1, le=100)
    num_chunks: Optional[int] = Field(default=10, ge=1, le=100)
    documentIds: NameList = None
    keys: NameList = None
    key: OptName = None


class DeleteMemoryRequest(BaseModel):
    namespace: Name
    delete_all: bool = False
    deleteAll: Optional[bool] = None


class DocumentItem(BaseModel):
    title: Title
    content: Content
    namespace: Name
    document_id: OptName = None
    documentId: OptName = None
    source_type: OptName = None
    sourceType: OptName = None
    metadata: OptMetadata = None
    priority: OptName = None
    created_at: Timestamp = None
    createdAt: Timestamp = None
    updated_at: Timestamp = None
    updatedAt: Timestamp = None


class InsertDocumentRequest(DocumentItem):
    pass


class BatchDocumentsRequest(BaseModel):
    items: Annotated[
        list[DocumentItem],
        Field(min_length=1, max_length=MAX_BATCH_ITEMS),
    ]


class QueryContextRequest(BaseModel):
    query: Content
    namespace: OptName = None
    includeReferences: Optional[bool] = None
    maxChunks: Optional[int] = Field(default=None, ge=1, le=100)
    document_ids: NameList = None
    documentIds: NameList = None
    recallOnly: Optional[bool] = None
    llmQuery: OptContent = None


class ChatRequest(BaseModel):
    messages: ChatMessages
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    maxTokens: Optional[int] = Field(default=None, ge=1, le=100_000)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=100_000)


class InteractionRequest(BaseModel):
    namespace: Name
    entityNames: NameList = None
    entity_names: NameList = None
    description: OptContent = None
    interactionLevel: OptName = None
    interaction_level: OptName = None
    interactionLevels: NameList = None
    interaction_levels: NameList = None
    timestamp: Timestamp = None


class ReinforceRequest(BaseModel):
    documentId: Name
    namespace: OptName = None


class PruneRequest(BaseModel):
    """Prune decayed memories below a retention threshold from one namespace."""
    namespace: Name
    minRetention: Optional[float] = Field(default=0.05, ge=0.0, le=1.0)
    min_retention: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dryRun: Optional[bool] = False
    dry_run: Optional[bool] = None
    keepPinned: Optional[bool] = True
    maxDelete: Optional[int] = Field(default=500, ge=0, le=10_000)


class ThoughtRequest(BaseModel):
    namespace: OptName = None
    maxChunks: Optional[int] = Field(default=10, ge=1, le=100)
    max_chunks: Optional[int] = Field(default=10, ge=1, le=100)
    temperature: Optional[float] = Field(default=0.3, ge=0.0, le=2.0)
    randomnessSeed: Optional[int] = None
    randomness_seed: Optional[int] = None
    persist: Optional[bool] = True
    enablePredictionCheck: Optional[bool] = None
    thoughtPrompt: OptContent = None
    thought_prompt: OptContent = None


class RecallMemoriesRequest(BaseModel):
    namespace: OptName = None
    topK: Optional[int] = Field(default=10, ge=0, le=100)
    top_k: Optional[int] = Field(default=10, ge=0, le=100)
    minRetention: Optional[float] = Field(default=0.0, ge=0.0, le=1.0)
    min_retention: Optional[float] = Field(default=0.0, ge=0.0, le=1.0)
    asOf: Timestamp = None
    as_of: Timestamp = None


class RecallMasterRequest(BaseModel):
    namespace: Name
    maxChunks: Optional[int] = Field(default=10, ge=1, le=100)
    max_chunks: Optional[int] = Field(default=10, ge=1, le=100)


class DataResponse(BaseModel):
    data: Any
