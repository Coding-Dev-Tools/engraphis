"""Deterministic free-text -> entity/relation graph feeder.

The general-purpose text->graph path v2 lacked: v2 had graph CRUD (``core.store``)
and a *code*-symbol indexer (``backends.codegraph``), and a *fact* extractor
(``backends.extractor``, text -> memory notes), but nothing that turned a free-text
memory into knowledge-graph **entities and relations**. This module adds it.

It ports v1's proven, dependency-free regex NER (``engraphis/engines/ingest.py``)
behind the same factory shape as ``get_extractor`` / ``get_embedder`` /
``get_reranker`` -- so it runs in the numpy-only offline gate and is opt-in by
configuration only (``ENGRAPHIS_GRAPH_EXTRACTOR = none | regex``). The default
``none`` -> ``NullGraphExtractor`` writes nothing, so the write path is byte-for-byte
unchanged unless explicitly enabled.

Security: extracted names are untrusted input (indirect prompt injection can steer
what lands in text; memory poisoning is an explicit threat model -- SECURITY.md).
Every extracted string is defanged (control/escape chars stripped, length-capped)
before it reaches the graph, the same rule ``service.py`` and the fact ``extractor``
apply to direct writes, and every node/edge is written scoped to the caller's
``(workspace_id, repo_id)`` -- it cannot cross the isolation boundary.
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from engraphis.core.interfaces import Edge, Node

# ── Regex NER (ported from engraphis/engines/ingest.py — the v1 heuristic path) ──
# Capitalized multi-word sequences, emails, hashtags, and mentions.  Keep the
# individual recognizers unambiguous: the old all-in-one expression could take
# polynomial time while backtracking over a long email-like local part.
_CAPITALIZED_WORD_RE = re.compile(r"\b[A-Z][a-z]+(?:-[A-Za-z]+)*\b")
_EMAIL_RE = re.compile(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_HASHTAG_RE = re.compile(r"#[a-zA-Z][a-zA-Z0-9_-]+")
_MENTION_RE = re.compile(r"@[a-zA-Z][a-zA-Z0-9_-]+")
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
    # Common capitalized sentence/workflow fragments. They are not stable identities
    # and otherwise become high-degree co-occurrence hubs in technical memories.
    "Active", "Action", "Actions", "Add", "Added", "All", "Also", "Artifact",
    "Artifacts", "Author", "Because", "Check", "Checked", "Comment", "Comments",
    "Commit", "Connection", "Connections", "Detail", "Details", "False", "Input",
    "Key", "Keys", "Local", "Manifest", "Merge", "Merged", "Missing", "Only",
    "Outcome", "Output", "Per", "Possible", "Reason", "Request", "Response", "Result",
    "Results", "Run", "Running", "Scan", "Scanned", "Status", "Test", "Tests",
    "Title", "True", "Verdict", "Approval", "Approved", "Categories", "Degraded",
    "Error", "Errors", "Failed", "Passed", "Rejected", "Skipped", "Success", "Verify",
    "Warning", "Warnings",
}
_STOPWORD_KEYS = {value.casefold() for value in _STOPWORDS}

# Extracted text is untrusted: strip the same control chars service.py strips from
# direct writes so an entity name can't smuggle a hidden-instruction / escape payload
# into the graph, and cap length so a pathological match can't bloat a node.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_NAME = 200
_MAX_ENTITIES = 128
_MAX_RELATIONS = 20
_MAX_COOCCUR_ENTITIES = 8      # cap pairwise co-occurrence per memory (<= 28 edges)
_COOCCUR_WEIGHT = 0.5          # weaker than a specific relation so PPR prefers real edges
# Leading determiners/articles stripped so "The Acme Corp" and "Acme Corp" collapse to one
# node. Deliberately NOT the full stopword set (which has months/days/pronouns we must not
# strip from real multi-word names like "May Smith").
_LEADING_DROP = {"The", "This", "That", "These", "Those", "A", "An",
                 "My", "Our", "Your", "Their", "His", "Her", "Its"}


def _defang(value: str) -> str:
    return _CONTROL_RE.sub("", value or "").strip()[:_MAX_NAME]


def _canon_concept(name: str) -> str:
    """Light, safe canonicalization for concept/person names so trivial variants collapse
    to one graph node: normalize whitespace, strip surrounding quotes, drop a possessive
    's, and strip a leading article. Conservative on purpose — aggressive merging (e.g.
    "Acme" == "Acme Corp") risks false merges and belongs in a resolution/LLM pass, not
    the free regex tier."""
    s = re.sub(r"\s+", " ", name or "").strip().strip("\"'`")
    s = re.sub(r"[’']s\b", "", s).strip()
    words = s.split()
    while len(words) > 1 and words[0] in _LEADING_DROP:
        words = words[1:]
    return " ".join(words).strip()


@dataclass
class GraphExtraction:
    """Result of one extraction pass: named entities and (src, relation, dst) triples."""
    entities: list[tuple[str, str]] = field(default_factory=list)      # (name, etype)
    relations: list[tuple[str, str, str]] = field(default_factory=list)  # (src, rel, dst)


def _extract_entities(text: str) -> list[tuple[str, str]]:
    text = text or ""

    def capitalized_candidates():
        # Assemble at most four whitespace-separated capitalized words, matching the
        # former heuristic without placing a nested repetition over untrusted text.
        words = iter(_CAPITALIZED_WORD_RE.finditer(text))
        current = next(words, None)
        while current is not None:
            first = last = current
            current = None
            for _ in range(3):
                following = next(words, None)
                if following is None:
                    break
                if text[last.end():following.start()].isspace():
                    last = following
                    continue
                current = following
                break
            else:
                current = next(words, None)
            yield (
                first.start(),
                0,
                last.end(),
                text[first.start():last.end()],
                "person_or_concept",
            )

    # Merge four already-position-ordered iterators. This keeps memory bounded even
    # for very large untrusted input and lets the entity fanout cap stop collection.
    candidates = heapq.merge(
        capitalized_candidates(),
        (
            (match.start(), 1, match.end(), match.group(0), "email")
            for match in _EMAIL_RE.finditer(text)
        ),
        (
            (match.start(), 2, match.end(), match.group(0), "hashtag")
            for match in _HASHTAG_RE.finditer(text)
        ),
        (
            (match.start(), 3, match.end(), match.group(0), "mention")
            for match in _MENTION_RE.finditer(text)
        ),
    )

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    matched_through = 0
    for start, _priority, end, candidate, etype in candidates:
        if start < matched_through:
            continue
        matched_through = end
        raw = candidate.strip()
        if not raw or raw.casefold() in _STOPWORD_KEYS or raw.casefold() in (
            "user", "the user"
        ):
            continue
        if etype == "person_or_concept":
            ent, etype = _canon_concept(raw), "person_or_concept"
            if len(ent) < 2 or ent.casefold() in _STOPWORD_KEYS:
                continue
        else:
            ent = raw
        key = ent.lower()
        if key not in seen:
            seen.add(key)
            out.append((ent, etype))
            if len(out) >= _MAX_ENTITIES:
                break
    return out


def _extract_entities_from_doc(title: str, content: str) -> list[tuple[str, str]]:
    """Extract from ``title`` and ``content`` as SEPARATE passes, then merge by name.

    Never concatenate first: the capitalized-word pattern has no notion of a
    title/content boundary, so matching ``f"{title}\\n\\n{content}"`` lets one match
    bridge the boundary (title "Meeting Notes" + content "Alice Johnson ..." -> one
    garbled node "Meeting Notes\\n\\nAlice Johnson"), fragmenting a real entity. This
    regression is covered by tests/test_graph_extractor.py.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for text in (content, title):   # content first — the stronger signal
        for ent, etype in _extract_entities(text):
            key = ent.lower()
            if key not in seen:
                seen.add(key)
                out.append((ent, etype))
                if len(out) >= _MAX_ENTITIES:
                    return out
    return out


def _extract_relations(text: str, entities: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Simple subject-relation-object triples via regex proximity (±60 chars)."""
    if len(entities) < 2:
        return []
    ent_names = [e[0] for e in entities]
    relations: list[tuple[str, str, str]] = []
    for m in _RELATION_RE.finditer(text or ""):
        rel = (m.group(0) or "").lower()
        window = text[max(0, m.start() - 60): m.end() + 60]
        nearby = [name for name in ent_names if name in window]
        if len(nearby) >= 2:
            relations.append((nearby[0], rel, nearby[1]))
    return relations[:_MAX_RELATIONS]


class NullGraphExtractor:
    """Default backend: extracts nothing, so the write path is unchanged."""

    def extract(self, content: str, *, title: str = "") -> GraphExtraction:
        return GraphExtraction()


class RegexGraphExtractor:
    """Dependency-free heuristic NER: capitalized names, emails, #tags, @mentions,
    plus proximity relations. No network, no model — safe in the offline gate."""

    def extract(self, content: str, *, title: str = "") -> GraphExtraction:
        entities = _extract_entities_from_doc(title, content)
        full_text = f"{title}\n\n{content}" if title else content
        relations = _extract_relations(full_text, entities)
        return GraphExtraction(entities=entities, relations=relations)


class StructuredMetadataGraphExtractor:
    """Graph extractor over ``ExtractedFact.metadata`` from ``llm_structured``.

    The structured fact extractor already validated the LLM payload before it became
    metadata, but metadata is still untrusted input at this boundary. This adapter is
    deliberately conservative: it accepts strings or ``{"name", "type"}`` entity
    objects, accepts common relation key aliases, adds relation endpoints as entities,
    and leaves final defanging / de-duplication to ``feed()``.
    """

    def __init__(self, metadata: Optional[dict] = None) -> None:
        self.metadata = metadata or {}

    def extract(self, content: str, *, title: str = "") -> GraphExtraction:
        raw_entities = self._items("entities")
        raw_relations = self._items("relations")
        entities: list[tuple[str, str]] = []
        canonical_by_key: dict[str, str] = {}

        def add_entity(name: str, etype: str = "person_or_concept") -> str:
            clean = _defang(name)
            if not clean:
                return ""
            key = clean.lower()
            canonical = canonical_by_key.get(key)
            if canonical is not None:
                return canonical
            canonical_by_key[key] = clean
            entities.append((clean, _defang(etype) or "person_or_concept"))
            return clean

        for item in raw_entities[:_MAX_COOCCUR_ENTITIES * 2]:
            if isinstance(item, str):
                add_entity(item)
            elif isinstance(item, dict):
                add_entity(str(item.get("name") or item.get("entity") or ""),
                           str(item.get("type") or item.get("etype") or "person_or_concept"))

        relations: list[tuple[str, str, str]] = []
        for item in raw_relations[:_MAX_RELATIONS]:
            if not isinstance(item, dict):
                continue
            src = str(item.get("source") or item.get("src") or item.get("from") or "")
            rel = str(item.get("relation") or item.get("type") or item.get("predicate") or "")
            dst = str(item.get("target") or item.get("dst") or item.get("to") or "")
            if not (src and rel and dst):
                continue
            src_name = add_entity(src)
            dst_name = add_entity(dst)
            if src_name and dst_name:
                relations.append((src_name, _defang(rel) or "related", dst_name))
        return GraphExtraction(entities=entities, relations=relations)

    def _items(self, key: str) -> list[Any]:
        direct = self.metadata.get(key)
        if isinstance(direct, list):
            return direct
        structured = self.metadata.get("structured_extraction")
        if isinstance(structured, dict) and isinstance(structured.get(key), list):
            return structured[key]
        return []


def get_graph_extractor(kind: str = "none"):
    """Factory mirroring ``get_extractor``: config in, backend out. ``kind='regex'``
    -> heuristic NER; anything else (incl. ``'none'``) -> the no-op passthrough."""
    if (kind or "none").lower() == "regex":
        return RegexGraphExtractor()
    return NullGraphExtractor()


def feed(store: Any, content: str, *, workspace_id: str, repo_id: Optional[str] = None,
         title: str = "", extractor: Any = None,
         provenance: Optional[dict] = None, commit: bool = True,
         extraction: Any = None) -> dict:
    """Extract entities/relations from free text and write them into the knowledge
    graph, scoped to ``(workspace_id, repo_id)``.

    * Entities are de-duplicated by the store (``upsert_entity`` returns the existing
      id for a repeat name/etype in the same scope).
    * Relations connect two extracted entities by their node ids, and are skipped if
      an equivalent live edge already exists — so re-feeding the same text is
      idempotent.
    * A ``None`` / ``NullGraphExtractor`` writes nothing and returns zero counts, so
      the default path is a genuine no-op.

    Returns ``{"entities": <written>, "relations": <written>}``.
    """
    extractor = extractor or NullGraphExtractor()
    result = extraction if extraction is not None else extractor.extract(content, title=title)

    name_to_id: dict[str, str] = {}
    for name, etype in result.entities[:_MAX_ENTITIES]:
        clean = _defang(name)
        if not clean or clean in name_to_id:
            continue
        name_to_id[clean] = store.upsert_entity(
            Node(id="", name=clean, ntype=_defang(etype),
                 workspace_id=workspace_id, repo_id=repo_id),
            commit=commit,
        )

    prov = dict(provenance or {})
    memory_id = str(prov.get("memory_id") or "")
    if memory_id:
        prior = prov.get("memory_ids")
        memory_ids = list(prior) if isinstance(prior, list) else []
        if memory_id not in memory_ids:
            memory_ids.append(memory_id)
        prov["memory_ids"] = memory_ids

    existing_edges = store.neighbors(list(name_to_id.values()))
    edge_by_key = {(e.src, e.dst, e.relation): e for e in existing_edges}
    specific_pairs: set[frozenset[str]] = set()
    written_relations = 0
    for src, rel, dst in result.relations:
        s, d = _defang(src), _defang(dst)
        sid, did = name_to_id.get(s), name_to_id.get(d)
        if not sid or not did or sid == did:
            continue
        relation = _defang(rel) or "related"
        key = (sid, did, relation)
        specific_pairs.add(frozenset((sid, did)))
        existing = edge_by_key.get(key)
        if existing is not None:
            store.add_edge_support(existing.id, prov, commit=commit)
            continue
        eid = store.upsert_edge(Edge(id="", src=sid, dst=did, relation=relation,
                                     workspace_id=workspace_id, repo_id=repo_id,
                                     provenance=prov), commit=commit)
        edge_by_key[key] = Edge(id=eid, src=sid, dst=did, relation=relation)
        written_relations += 1

    # Co-occurrence edges connect entities that share this memory unless this memory
    # already supplied a specific relation for the pair. Keep co-occurrence support
    # separate from a specific relation learned from some other memory, so retiring
    # either source cannot erase the other source's weaker graph evidence.
    ids = list(name_to_id.values())[:_MAX_COOCCUR_ENTITIES]
    if len(ids) >= 2:
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                pair = frozenset((a, b))
                if a == b or pair in specific_pairs:
                    continue
                lo, hi = (a, b) if a < b else (b, a)
                key = (lo, hi, "co_occurs")
                existing = edge_by_key.get(key)
                if existing is not None:
                    store.add_edge_support(existing.id, prov, commit=commit)
                    continue
                eid = store.upsert_edge(Edge(
                    id="", src=lo, dst=hi, relation="co_occurs",
                    weight=_COOCCUR_WEIGHT, workspace_id=workspace_id,
                    repo_id=repo_id, provenance=prov,
                ), commit=commit)
                edge_by_key[key] = Edge(id=eid, src=lo, dst=hi, relation="co_occurs")
                written_relations += 1

    return {"entities": len(name_to_id), "relations": written_relations}
