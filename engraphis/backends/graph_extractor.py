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

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from engraphis.core.interfaces import Edge, Node

# ── Regex NER (ported from engraphis/engines/ingest.py — the v1 heuristic path) ──
# Capitalized multi-word sequences, emails, URLs/hashtags, quoted names/mentions.
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
}

# Extracted text is untrusted: strip the same control chars service.py strips from
# direct writes so an entity name can't smuggle a hidden-instruction / escape payload
# into the graph, and cap length so a pathological match can't bloat a node.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_NAME = 200
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
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _ENTITY_RE.finditer(text or ""):
        raw = (m.group(0) or "").strip()
        if not raw or raw in _STOPWORDS or raw.lower() in ("user", "the user"):
            continue
        if raw.startswith("#"):
            ent, etype = raw, "hashtag"
        elif raw.startswith("@"):
            ent, etype = raw, "mention"
        elif "@" in raw and "." in raw:
            ent, etype = raw, "email"
        else:
            ent, etype = _canon_concept(raw), "person_or_concept"
            if len(ent) < 2 or ent in _STOPWORDS:
                continue
        key = ent.lower()
        if key not in seen:
            seen.add(key)
            out.append((ent, etype))
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


def get_graph_extractor(kind: str = "none"):
    """Factory mirroring ``get_extractor``: config in, backend out. ``kind='regex'``
    -> heuristic NER; anything else (incl. ``'none'``) -> the no-op passthrough."""
    if (kind or "none").lower() == "regex":
        return RegexGraphExtractor()
    return NullGraphExtractor()


def feed(store: Any, content: str, *, workspace_id: str, repo_id: Optional[str] = None,
         title: str = "", extractor: Any = None,
         provenance: Optional[dict] = None) -> dict:
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
    result = extractor.extract(content, title=title)

    name_to_id: dict[str, str] = {}
    for name, etype in result.entities:
        clean = _defang(name)
        if not clean or clean in name_to_id:
            continue
        name_to_id[clean] = store.upsert_entity(
            Node(id="", name=clean, ntype=_defang(etype),
                 workspace_id=workspace_id, repo_id=repo_id)
        )

    prov = dict(provenance or {})
    written_relations = 0
    for src, rel, dst in result.relations:
        s, d = _defang(src), _defang(dst)
        sid, did = name_to_id.get(s), name_to_id.get(d)
        if not sid or not did or sid == did:
            continue
        existing = {(e.src, e.dst, e.relation) for e in store.neighbors([sid])}
        relation = _defang(rel) or "related"
        if (sid, did, relation) in existing:
            continue
        store.upsert_edge(Edge(id="", src=sid, dst=did, relation=relation,
                               workspace_id=workspace_id, repo_id=repo_id,
                               provenance=prov))
        written_relations += 1

    # Co-occurrence edges: join entities that share this memory but aren't already
    # linked by a specific relation. The regex extractor finds few proximity relations,
    # so without this the graph is a dust cloud of isolated nodes — the Graph tab hides
    # them ("Hide unconnected") and the PPR recall arm has nothing to walk. Bounded,
    # written in canonical id order (idempotent), weak weight so real relations dominate.
    ids = list(name_to_id.values())[:_MAX_COOCCUR_ENTITIES]
    if len(ids) >= 2:
        linked = {frozenset((e.src, e.dst)) for e in store.neighbors(ids)}
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if a == b or frozenset((a, b)) in linked:
                    continue
                lo, hi = (a, b) if a < b else (b, a)
                store.upsert_edge(Edge(id="", src=lo, dst=hi, relation="co_occurs",
                                       weight=_COOCCUR_WEIGHT, workspace_id=workspace_id,
                                       repo_id=repo_id, provenance=prov))
                linked.add(frozenset((a, b)))
                written_relations += 1

    return {"entities": len(name_to_id), "relations": written_relations}
