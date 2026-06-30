"""Pydantic request/response models mirroring the Engraphis SDK contract."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    key: str
    content: str
    namespace: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class InsertMemoryRequest(BaseModel):
    item: Optional[MemoryItem] = None
    items: Optional[list[MemoryItem]] = None
    key: Optional[str] = None
    content: Optional[str] = None
    namespace: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class QueryMemoryRequest(BaseModel):
    query: Optional[str] = None
    prompt: Optional[str] = None
    namespace: Optional[str] = None
    maxChunks: Optional[int] = 10
    num_chunks: Optional[int] = 10
    documentIds: Optional[list[str]] = None
    keys: Optional[list[str]] = None
    key: Optional[str] = None


class DeleteMemoryRequest(BaseModel):
    namespace: str
    delete_all: bool = False
    deleteAll: Optional[bool] = None


class DocumentItem(BaseModel):
    title: str
    content: str
    namespace: str
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
    namespace: Optional[str] = None
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
    namespace: str
    entityNames: list[str]
    entity_names: Optional[list[str]] = None
    description: Optional[str] = None
    interactionLevel: Optional[str] = None
    interaction_level: Optional[str] = None
    interactionLevels: Optional[list[str]] = None
    interaction_levels: Optional[list[str]] = None
    timestamp: Optional[float] = None


class ThoughtRequest(BaseModel):
    namespace: Optional[str] = None
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
    namespace: Optional[str] = None
    topK: Optional[float] = 10
    top_k: Optional[int] = 10
    minRetention: Optional[float] = 0.0
    min_retention: Optional[float] = 0.0
    asOf: Optional[float] = None
    as_of: Optional[float] = None


class RecallMasterRequest(BaseModel):
    namespace: str
    maxChunks: Optional[int] = 10
    max_chunks: Optional[int] = 10


class DataResponse(BaseModel):
    data: Any
