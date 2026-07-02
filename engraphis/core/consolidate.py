"""Sleep-time consolidation (MASTER_PLAN.md Phase 4 — episodic→semantic distillation).

Letta ships "sleep-time compute" as a cloud service; the local-first equivalent is a
background job the *user* schedules (cron / Windows Task Scheduler / a session hook):

    python -m scripts.consolidate --db engraphis.db --workspace acme

Two passes, both governed by the house rules (never a hard delete, everything audited,
provenance always):

1. **Distill** — clusters of recurring episodic memories on the same subject (token
   Jaccard, same signal the write-path resolver uses) become one durable *semantic*
   digest that links back to every source. Deterministic by default; pass an LLM to
   write a nicer summary (the digest falls back to the deterministic text on any error).
2. **Archive** — transient memories (working/episodic) whose Ebbinghaus retention has
   decayed below a floor are bi-temporally closed (``close_validity``), not deleted:
   they leave the live view but remain in history for ``why``/``timeline``. Pinned
   memories are always exempt (AGENTS.md §3.2).

Pure ``numpy``-only core; runs fully offline.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.textutil import jaccard, tokenize

# Cluster admission: same-subject signal, deliberately the resolver's threshold.
SUBJECT_JACCARD = 0.40
# Minimum recurrences before an episodic pattern is worth a semantic digest.
MIN_CLUSTER = 3
# Retention floor for archiving transient memories (exp(-Δt/S) — see scoring.retention).
ARCHIVE_BELOW = 0.05
# How many source lines the deterministic digest quotes.
DIGEST_QUOTES = 5

_DIGEST_SYSTEM_PROMPT = (
    "You consolidate recurring episodic agent memories into one durable semantic fact. "
    "Respond with 1-3 plain sentences capturing the stable pattern — no preamble, no "
    "markdown, no speculation beyond what the entries state."
)


def consolidate(engine, *, workspace_id: str, repo_id: Optional[str] = None,
                min_cluster: int = MIN_CLUSTER, subject_jaccard: float = SUBJECT_JACCARD,
                archive_below: float = ARCHIVE_BELOW, dry_run: bool = False,
                llm: Any = None, now: Optional[float] = None) -> dict:
    """Run one consolidation sweep over a workspace (optionally one repo). Returns a
    JSON-able report; with ``dry_run=True`` it only reports what *would* happen."""
    store = engine.store
    now = now or time.time()
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)

    episodic = [m for m in store.list_memories(flt, limit=2000)
                if m.mtype == MemoryType.EPISODIC]
    clusters = _cluster_by_subject(episodic, threshold=subject_jaccard)

    report: dict = {"workspace_id": workspace_id, "repo_id": repo_id, "dry_run": dry_run,
                    "clusters_found": 0, "digests_created": [], "archived": [],
                    "skipped_already_consolidated": 0}

    # ── pass 1: distill recurring episodes into semantic digests ─────────────
    for cluster in clusters:
        if len(cluster) < min_cluster:
            continue
        report["clusters_found"] += 1
        if any(_already_consolidated(store, m.id) for m in cluster):
            report["skipped_already_consolidated"] += 1
            continue
        if dry_run:
            report["digests_created"].append(
                {"would_consolidate": [m.id for m in cluster]})
            continue
        digest_id = _write_digest(engine, cluster, llm=llm, now=now)
        report["digests_created"].append(
            {"id": digest_id, "consolidates": [m.id for m in cluster]})

    # ── pass 2: archive fully-decayed transient memories ─────────────────────
    for m in store.list_memories(flt, limit=2000):
        if m.mtype not in (MemoryType.WORKING, MemoryType.EPISODIC) or m.pinned:
            continue
        r = scoring.retention(m.stability, m.last_access, now)
        if r >= archive_below:
            continue
        report["archived"].append({"id": m.id, "retention": round(r, 4)})
        if not dry_run:
            store.close_validity(
                m.id, actor="consolidation",
                reason=f"retention {r:.4f} below {archive_below} (consolidation sweep)")
            try:
                engine.index.delete([m.id])
            except Exception:
                pass

    return report


# ── internals ─────────────────────────────────────────────────────────────────

def _cluster_by_subject(memories: list[MemoryRecord], *, threshold: float) -> list[list[MemoryRecord]]:
    """Greedy single-link clustering on token Jaccard — deterministic, order-stable
    (memories arrive newest-first from the store; clusters keep that order)."""
    token_sets = [tokenize(f"{m.title} {m.content}") for m in memories]
    n = len(memories)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard(token_sets[i], token_sets[j]) >= threshold:
                parent[find(i)] = find(j)

    groups: dict[int, list[MemoryRecord]] = {}
    for i, m in enumerate(memories):
        groups.setdefault(find(i), []).append(m)
    return list(groups.values())


def _already_consolidated(store, memory_id: str) -> bool:
    return any(link["relation"] == "consolidates" for link in store.get_links(memory_id))


def _common_tokens(cluster: list[MemoryRecord], k: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for m in cluster:
        for t in tokenize(f"{m.title} {m.content}"):
            counts[t] = counts.get(t, 0) + 1
    shared = [t for t, c in counts.items() if c >= max(2, len(cluster) // 2 + 1)]
    return sorted(shared, key=lambda t: (-counts[t], t))[:k]


def _write_digest(engine, cluster: list[MemoryRecord], *, llm: Any, now: float) -> str:
    subject = ", ".join(_common_tokens(cluster)) or "recurring episode"
    quotes = [m.content.strip().replace("\n", " ")[:300] for m in cluster[:DIGEST_QUOTES]]
    content = (f"Recurring pattern ({len(cluster)} occurrences): {subject}.\n"
               + "\n".join(f"- {q}" for q in quotes))
    if llm is not None:
        try:
            body = "\n".join(f"- {m.content.strip()}" for m in cluster)
            if hasattr(llm, "chat"):
                summary = llm.chat([{"role": "user", "content": body}],
                                   system=_DIGEST_SYSTEM_PROMPT)
            else:
                summary = llm.complete([{"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
                                        {"role": "user", "content": body}])
            # LLM output is untrusted (same rule as backends.extractor): defang it.
            summary = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", summary or "")
            summary = summary.strip()[:10_000]
            if summary:
                content = f"{summary}\n\n(Consolidated from {len(cluster)} episodes: {subject})"
        except Exception:
            pass  # deterministic digest already in place

    first = cluster[0]
    importance = max([m.importance or 0.0 for m in cluster] + [0.5])
    digest_id = engine.remember(
        content,
        workspace_id=first.workspace_id, repo_id=first.repo_id,
        mtype=MemoryType.SEMANTIC, scope=Scope(first.scope),
        title=f"Consolidated: {subject}"[:200], importance=importance,
        keywords=_common_tokens(cluster, k=8),
        metadata={"provenance": {"source": "consolidation",
                                 "consolidates": [m.id for m in cluster]}},
        resolve_conflicts=False,   # the digest is new by construction
    )
    for m in cluster:
        engine.store.add_link(digest_id, m.id, "consolidates")
    engine.store.audit("consolidation", "distill", digest_id,
                       f"digested {len(cluster)} episodic memories")
    return digest_id
