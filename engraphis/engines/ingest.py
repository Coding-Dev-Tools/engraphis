"""Ingestion engine — Phase 1 of the consciousness loop.

Takes raw content, chunks it, embeds it, extracts entities/relations (lightweight
NER via regex + keyword heuristics, upgradeable to LLM-based extraction), appends
a state-transition event, and stores everything in the memory layer.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import numpy as np

from engraphis.engines import embedder
from engraphis.stores import now_ts
from engraphis.stores import graph as graph_store
from engraphis.stores import ledger as ledger_store
from engraphis.stores import vectors as mem_store

# ── Lightweight entity extraction ────────────────────────────────────────────
# Capitalized multi-word sequences, emails, URLs, hashtags, quoted names.
_ENTITY_RE = re.compile(
    r"\b([A-Z][a-z]+(?:-[A-Za-z]+)*(?:\s+[A-Z][a-z]+(?:-[A-Za-z]+)*){0,3})\b"
    r"|([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
    r"|(#[a-zA-Z][a-zA-Z0-9_-]+)"
    r"|(@[a-zA-Z][a-zA-Z0-9_-]+)"
)
_RELATION_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|owns|works at|lives in|prefers|likes|"
    r"dislikes|uses|manages|created|founded|located in|part of|member of)\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "The", "This", "That", "These", "Those", "A", "An", "And", "But", "Or",
    "If", "Then", "When", "Where", "What", "Who", "How", "Why", "It", "Is",
    "Was", "Are", "Were", "Has", "Have", "Had", "Will", "Would", "Could",
    "Should", "May", "Might", "Can", "Did", "Do", "Does", "Not", "No", "Yes",
    "User", "We", "They", "He", "She", "His", "Her", "Their", "Our", "My",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    # Common sentence-start words that match the capitalized-entity regex.
    "New", "First", "Last", "Next", "Let", "Now", "Here", "There", "Also",
    "However", "Before", "After", "Since", "While", "Although", "Because",
    "Just", "Still", "Even", "Already", "Another", "Each", "Every", "Both",
    "Many", "Some", "Most", "Few", "All", "Any", "Either", "Neither",
    "Please", "Thanks", "Note", "See", "Tip", "Warning", "Important",
    "Example", "Step", "Section", "Chapter", "Figure", "Table",
    "For", "From", "To", "In", "On", "At", "By", "With", "About", "Between",
    "Through", "During", "After", "Above", "Below", "Under", "Over",
}


def ingest_document(
    *,
    namespace: str,
    document_id: str,
    title: str,
    content: str,
    metadata: Optional[dict] = None,
    source_type: Optional[str] = None,
    priority: Optional[str] = None,
    created_at: Optional[float] = None,
    updated_at: Optional[float] = None,
    memory_type: str = "semantic",
    vector: Optional[np.ndarray] = None,
) -> dict[str, Any]:
    """Full ingestion pipeline: embed (or use provided vector) → store → extract entities → append event."""
    ts = now_ts()
    created_at = created_at or ts
    updated_at = updated_at or ts

    full_text = f"{title}\n\n{content}" if title else content
    vec = vector if vector is not None else embedder.embed(full_text)

    mem = mem_store.upsert_memory(
        namespace=namespace,
        document_id=document_id,
        title=title,
        content=content,
        metadata=metadata,
        source_type=source_type,
        priority=priority,
        vector=vec,
        created_at=created_at,
        updated_at=updated_at,
        memory_type=memory_type,
    )

    entities = _extract_entities_from_doc(title, content)
    for name, etype in entities:
        graph_store.upsert_entity(namespace, name, etype)
        ledger_store.append_event(
            namespace=namespace,
            entity_name=name,
            event_type="ingest",
            description=f"Entity seen in document '{title}'",
            payload={"document_id": document_id, "entity_type": etype},
            timestamp=updated_at,
        )

    relations = _extract_relations(full_text, entities)
    for src, rel, tgt in relations:
        graph_store.upsert_edge(namespace, src, tgt, rel)

    job = ledger_store.create_job(
        namespace=namespace,
        job_type="ingest",
        payload={"document_id": document_id, "entity_count": len(entities), "edge_count": len(relations)},
    )

    return {
        **mem,
        "jobId": job["job_id"],
        "status": "inserted" if mem.get("access_count", 0) == 0 else "updated",
        "entities": len(entities),
        "edges": len(relations),
    }


def ingest_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Ingest multiple documents. Each item must have title, content, namespace, document_id."""
    results = []
    for item in items:
        results.append(ingest_document(
            namespace=item["namespace"],
            document_id=item.get("documentId", item.get("document_id")),
            title=item.get("title", ""),
            content=item["content"],
            metadata=item.get("metadata"),
            source_type=item.get("sourceType", item.get("source_type")),
            priority=item.get("priority"),
            created_at=item.get("createdAt", item.get("created_at")),
            updated_at=item.get("updatedAt", item.get("updated_at")),
            memory_type=item.get("memory_type", item.get("memoryType", "semantic")),
        ))
    job = ledger_store.create_job(
        namespace=None,
        job_type="batch_ingest",
        payload={"count": len(results)},
    )
    return {"accepted": results, "jobId": job["job_id"], "count": len(results)}


# ── Entity / relation extraction (heuristic, no LLM needed) ──────────────────

def _extract_entities(text: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _ENTITY_RE.finditer(text):
        raw = m.group(0).strip()
        if not raw or raw in _STOPWORDS:
            continue
        if raw.lower() in ("user", "the user"):
            continue
        if raw.startswith("#"):
            ent, etype = raw, "hashtag"
        elif raw.startswith("@") and "@" in raw and "." in raw:
            ent, etype = raw, "email"
        elif raw.startswith("@"):
            ent, etype = raw, "mention"
        elif "@" in raw and "." in raw and not raw.startswith("@"):
            ent, etype = raw, "email"
        else:
            ent, etype = raw, "person_or_concept"
        key = ent.lower()
        if key not in seen:
            seen.add(key)
            out.append((ent, etype))
    return out


def _extract_entities_from_doc(title: str, content: str) -> list[tuple[str, str]]:
    """Extract entities from title and content independently, then merge.

    Title and content must be processed as *separate* regex passes, never
    concatenated first: the capitalized-word pattern in ``_ENTITY_RE`` has no notion
    of a title/content boundary, so matching it against ``f"{title}\\n\\n{content}"``
    lets it bridge across that boundary — e.g. title "Meeting Notes" + content
    "Alice Johnson met..." previously produced one garbled entity "Meeting
    Notes\\n\\nAlice Johnson" instead of two clean ones. That fragments what should
    be a single real-world entity (e.g. "Alice Johnson") into multiple graph nodes,
    each of which only sees the document(s) it happened to be garbled together with
    — which is why clicking a node could open the wrong document or none at all.
    Extracting each field on its own and merging by name avoids that entirely.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for text in (content, title):   # content first — the more meaningful signal
        for ent, etype in _extract_entities(text):
            key = ent.lower()
            if key not in seen:
                seen.add(key)
                out.append((ent, etype))
    return out


def _extract_relations(text: str, entities: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Find simple subject-relation-object triples via regex proximity."""
    if len(entities) < 2:
        return []
    ent_names = [e[0] for e in entities]
    relations: list[tuple[str, str, str]] = []
    for m in _RELATION_RE.finditer(text):
        rel = m.group(0).lower()
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        window = text[start:end]
        nearby = []
        for name in ent_names:
            if name in window:
                nearby.append(name)
        if len(nearby) >= 2:
            relations.append((nearby[0], rel, nearby[1]))
    return relations[:20]
