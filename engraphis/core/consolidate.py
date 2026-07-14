"""Sleep-time consolidation (episodic→semantic distillation).

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
from engraphis.core.textutil import estimate_tokens, jaccard, tokenize

# Cluster admission: same-subject signal, deliberately the resolver's threshold.
SUBJECT_JACCARD = 0.40
# Minimum recurrences before an episodic pattern is worth a semantic digest.
MIN_CLUSTER = 3
# Retention floor for archiving transient memories (exp(-Δt/S) — see scoring.retention).
ARCHIVE_BELOW = 0.05
# How many source lines the deterministic digest quotes.
DIGEST_QUOTES = 5
# Minimum live memories mentioning an entity before it earns a rolled-up profile.
MIN_PROFILE_MENTIONS = 3
# Skip 1-2 char entity names — too noisy to profile reliably.
PROFILE_MIN_NAME_LEN = 3
# Relation linking a profile digest back to every memory it summarizes.
PROFILE_RELATION = "profiles"
# How many source lines the deterministic profile quotes.
PROFILE_QUOTES = 6

# Associative cross-cluster inference (dream pass 4): connect memories in *different,
# dissimilar* subject clusters that share a bridging entity. Deliberately conservative —
# it proposes an evidence-only link, never a synthesized new fact; dry-run by default; low
# salience; never trusted; capped fan-out. This is the "connect distant dots" step.
INFER_MIN_CLUSTERS = 2       # entity must appear across at least this many distinct clusters
INFER_MAX_LINKS = 20         # cap proposals per sweep (fan-out guard)
INFER_IMPORTANCE = 0.25      # inferred links are low-salience by construction
INFER_RELATION = "related_by_inference"

_DIGEST_SYSTEM_PROMPT = (
    "You consolidate recurring episodic agent memories into one durable semantic fact. "
    "Respond with 1-3 plain sentences capturing the stable pattern — no preamble, no "
    "markdown, no speculation beyond what the entries state."
)
_PROFILE_SYSTEM_PROMPT = (
    "You consolidate everything known about one subject into a compact profile. "
    "Respond with 2-4 plain sentences stating the durable facts and preferences about "
    "the subject — no preamble, no markdown, no speculation beyond what the entries state."
)
_INFER_SYSTEM_PROMPT = (
    "You are given two or more notes that share a common entity. State, in ONE sentence, the "
    "connection they suggest — grounded strictly in what the notes say, with no speculation. "
    "No preamble, no markdown."
)


def _mem_tokens(m: MemoryRecord) -> int:
    """Estimated context cost of one memory (title + body)."""
    return estimate_tokens(f"{m.title} {m.content}")


def _compaction(tokens_before: int, tokens_after: int, units: int) -> dict:
    """A JSON-able before/after token summary — the number that proves a sweep
    shrank how much history an agent must carry in context (AGENTS.md §3.7)."""
    saved = max(0, tokens_before - tokens_after)
    pct = round(100.0 * saved / tokens_before, 1) if tokens_before else 0.0
    return {"tokens_before": tokens_before, "tokens_after": tokens_after,
            "tokens_saved": saved, "reduction_pct": pct, "units": units}


def consolidate(engine, *, workspace_id: str, repo_id: Optional[str] = None,
                min_cluster: int = MIN_CLUSTER, subject_jaccard: float = SUBJECT_JACCARD,
                archive_below: float = ARCHIVE_BELOW, dry_run: bool = False,
                profiles: bool = False, min_mentions: int = MIN_PROFILE_MENTIONS,
                infer: bool = False, llm: Any = None, now: Optional[float] = None) -> dict:
    """Run one consolidation sweep over a workspace (optionally one repo). Returns a
    JSON-able report; with ``dry_run=True`` it only reports what *would* happen.

    Every pass reports its **compaction** — the estimated context tokens before vs.
    after — so a sweep's payoff is a number, not a claim (AGENTS.md §3.7). With
    ``profiles=True`` a third pass additionally rolls each entity's scattered memories
    into one durable profile digest (per-entity profile digests); its report lands
    under ``report["profiles"]``.
    """
    store = engine.store
    now = now or time.time()
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)

    episodic = [m for m in store.list_memories(flt, limit=2000)
                if m.mtype == MemoryType.EPISODIC]
    clusters = _cluster_by_subject(episodic, threshold=subject_jaccard)

    report: dict = {"workspace_id": workspace_id, "repo_id": repo_id, "dry_run": dry_run,
                    "clusters_found": 0, "digests_created": [], "archived": [],
                    "skipped_already_consolidated": 0}
    distilled_before = distilled_after = 0
    archived_tokens = 0

    # ── pass 1: distill recurring episodes into semantic digests ─────────────
    for cluster in clusters:
        if len(cluster) < min_cluster:
            continue
        report["clusters_found"] += 1
        if any(_already_consolidated(store, m.id) for m in cluster):
            report["skipped_already_consolidated"] += 1
            continue
        content, subject = _build_digest_content(cluster, llm=llm)
        t_before = sum(_mem_tokens(m) for m in cluster)
        t_after = estimate_tokens(content)
        distilled_before += t_before
        distilled_after += t_after
        entry = {"consolidates": [m.id for m in cluster],
                 **_compaction(t_before, t_after, len(cluster))}
        if dry_run:
            entry["would_consolidate"] = entry.pop("consolidates")
        else:
            entry["id"] = _write_digest(engine, cluster, content=content,
                                        subject=subject, now=now)
        report["digests_created"].append(entry)

    # ── pass 2: archive fully-decayed transient memories ─────────────────────
    for m in store.list_memories(flt, limit=2000):
        if m.mtype not in (MemoryType.WORKING, MemoryType.EPISODIC) or m.pinned:
            continue
        r = scoring.retention(m.stability, m.last_access, now)
        if r >= archive_below:
            continue
        archived_tokens += _mem_tokens(m)
        report["archived"].append({"id": m.id, "retention": round(r, 4),
                                   "tokens_freed": _mem_tokens(m)})
        if not dry_run:
            store.close_validity(
                m.id, actor="consolidation",
                reason=f"retention {r:.4f} below {archive_below} (consolidation sweep)")
            try:
                engine.index.delete([m.id])
            except Exception:
                pass

    # ── compaction summary: the payoff of the sweep, as a number ─────────────
    report["compaction"] = {
        "distilled": _compaction(distilled_before, distilled_after,
                                 len(report["digests_created"])),
        "archived_tokens_freed": archived_tokens,
        "total_tokens_saved": max(0, distilled_before - distilled_after) + archived_tokens,
    }

    # ── pass 3 (opt-in): roll each entity's memories into one profile ─────────
    if profiles:
        report["profiles"] = consolidate_profiles(
            engine, workspace_id=workspace_id, repo_id=repo_id,
            min_mentions=min_mentions, dry_run=dry_run, llm=llm, now=now)

    # ── pass 4 (opt-in): associative cross-cluster inference ─────────────────
    if infer:
        # The inference pass follows the sweep's own ``dry_run`` flag: a dry-run sweep
        # proposes into the report; a real sweep applies the low-salience, untrusted,
        # linked memories. It is OFF by default (``infer=False``) so a human opts in —
        # the safety property is "off by default", not "dry-run by default".
        report["inferences"] = infer_links(
            engine, workspace_id=workspace_id, repo_id=repo_id,
            subject_jaccard=subject_jaccard, dry_run=dry_run, llm=llm, now=now)

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


def _llm_summary(llm: Any, system_prompt: str, body: str) -> Optional[str]:
    """Ask an optional LLM for a summary, defanged. Returns ``None`` on any error or
    empty result so callers keep their deterministic text. LLM output is untrusted
    (same rule as ``backends.extractor``): strip control/escape chars, length-cap."""
    try:
        if hasattr(llm, "chat"):
            summary = llm.chat([{"role": "user", "content": body}], system=system_prompt)
        else:
            summary = llm.complete([{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": body}])
        summary = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", summary or "").strip()[:10_000]
        return summary or None
    except Exception:
        return None


def _build_digest_content(cluster: list[MemoryRecord], *, llm: Any) -> tuple[str, str]:
    """The digest text + its subject label. Deterministic by default; an optional LLM
    writes a nicer summary but falls back to the deterministic text on any error, so the
    content (and thus its token estimate) is knowable without writing anything."""
    subject = ", ".join(_common_tokens(cluster)) or "recurring episode"
    quotes = [m.content.strip().replace("\n", " ")[:300] for m in cluster[:DIGEST_QUOTES]]
    content = (f"Recurring pattern ({len(cluster)} occurrences): {subject}.\n"
               + "\n".join(f"- {q}" for q in quotes))
    if llm is not None:
        summary = _llm_summary(llm, _DIGEST_SYSTEM_PROMPT,
                               "\n".join(f"- {m.content.strip()}" for m in cluster))
        if summary:
            content = f"{summary}\n\n(Consolidated from {len(cluster)} episodes: {subject})"
    return content, subject


def _write_digest(engine, cluster: list[MemoryRecord], *, content: str, subject: str,
                  now: float) -> str:
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


# ── pass 3: entity profiles (a "profile that grows with you") ────────

def consolidate_profiles(engine, *, workspace_id: str, repo_id: Optional[str] = None,
                         min_mentions: int = MIN_PROFILE_MENTIONS, dry_run: bool = False,
                         llm: Any = None, now: Optional[float] = None) -> dict:
    """Roll every live memory that mentions an entity into one durable *profile* digest
    — a local-first per-entity knowledge profile that grows with use.

    Deterministic and offline: entities come from the knowledge graph
    (``store.list_entities``); a memory belongs to an entity's profile if the entity's
    name occurs in its title/content (case-insensitive), within the same scope and the
    default (live) validity window. A profile is a ``semantic`` memory linked to every
    source via ``profiles`` and provenance ``source='profile_consolidation'``.

    Idempotent (mirrors the distill pass): if any candidate source is already in a
    profile, the entity is skipped rather than re-summarized. Governed like every other
    consolidation write — audited, never a hard delete, scoped to the caller's workspace.
    """
    store = engine.store
    now = now or time.time()
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
    report: dict = {"workspace_id": workspace_id, "repo_id": repo_id, "dry_run": dry_run,
                    "entities_considered": 0, "profiles_created": [], "skipped_existing": 0}

    live = [m for m in store.list_memories(flt, limit=5000)
            if m.mtype in (MemoryType.EPISODIC, MemoryType.SEMANTIC)
            and m.metadata.get("provenance", {}).get("source") != "profile_consolidation"]
    p_before = p_after = 0

    for ent in store.list_entities(flt, limit=2000):
        name = (ent.name or "").strip()
        if len(name) < PROFILE_MIN_NAME_LEN:
            continue
        needle = name.lower()
        sources = [m for m in live if needle in f"{m.title} {m.content}".lower()]
        if len(sources) < min_mentions:
            continue
        report["entities_considered"] += 1
        if any(_in_profile(store, m.id) for m in sources):
            report["skipped_existing"] += 1
            continue
        content = _build_profile_content(name, ent.ntype, sources, llm=llm)
        t_before = sum(_mem_tokens(m) for m in sources)
        t_after = estimate_tokens(content)
        p_before += t_before
        p_after += t_after
        entry = {"entity": name, "etype": ent.ntype, "mentions": len(sources),
                 **_compaction(t_before, t_after, len(sources))}
        if dry_run:
            entry["would_profile"] = [m.id for m in sources]
        else:
            entry["id"] = _write_profile(engine, name, ent.ntype, sources,
                                         content=content, now=now)
        report["profiles_created"].append(entry)

    report["compaction"] = _compaction(p_before, p_after, len(report["profiles_created"]))
    return report


def _in_profile(store, memory_id: str) -> bool:
    return any(link["relation"] == PROFILE_RELATION for link in store.get_links(memory_id))


def _build_profile_content(name: str, etype: str, sources: list[MemoryRecord],
                           *, llm: Any) -> str:
    label = f"{name} ({etype})" if etype else name
    quotes = [m.content.strip().replace("\n", " ")[:300] for m in sources[:PROFILE_QUOTES]]
    content = (f"Profile — {label}: {len(sources)} references.\n"
               + "\n".join(f"- {q}" for q in quotes))
    if llm is not None:
        summary = _llm_summary(
            llm, _PROFILE_SYSTEM_PROMPT,
            f"Subject: {name}\n" + "\n".join(f"- {m.content.strip()}" for m in sources))
        if summary:
            content = f"{summary}\n\n(Profile of {label}, from {len(sources)} memories)"
    return content


def _write_profile(engine, name: str, etype: str, sources: list[MemoryRecord],
                   *, content: str, now: float) -> str:
    first = sources[0]
    importance = max([m.importance or 0.0 for m in sources] + [0.6])
    profile_id = engine.remember(
        content,
        workspace_id=first.workspace_id, repo_id=first.repo_id,
        mtype=MemoryType.SEMANTIC, scope=Scope(first.scope),
        title=f"Profile: {name}"[:200], importance=importance,
        keywords=[name] + _common_tokens(sources, k=6),
        metadata={"provenance": {"source": "profile_consolidation", "entity": name,
                                 "etype": etype, "profiles": [m.id for m in sources]}},
        resolve_conflicts=False,   # a profile is new by construction
    )
    for m in sources:
        engine.store.add_link(profile_id, m.id, PROFILE_RELATION)
    engine.store.audit("consolidation", "profile", profile_id,
                       f"profiled {len(sources)} memories about {name}")
    return profile_id


# ── pass 4: associative cross-cluster inference (the "connect distant dots" step) ──

def infer_links(engine, *, workspace_id: str, repo_id: Optional[str] = None,
                subject_jaccard: float = SUBJECT_JACCARD, max_links: int = INFER_MAX_LINKS,
                dry_run: bool = True, llm: Any = None, now: Optional[float] = None) -> dict:
    """Connect memories that sit in *different, dissimilar* subject clusters but share a
    bridging entity — the associative step ordinary consolidation (same-subject distill)
    never reaches.

    It never fabricates a claim: an inferred memory states only that a shared entity
    connects two otherwise-separate topics, and quotes both sides. Written memories are
    low-salience, ``trusted:false``, ``source='dream_inference'``, and linked back to their
    sources, so a bad inference is visible, downweighted, and never merge-eligible into a
    trusted fact (SECURITY.md — memory poisoning). ``dry_run=True`` (default) only proposes;
    fan-out is capped at ``max_links``. Deterministic and offline; an optional LLM only
    rephrases the connection and fails soft to the deterministic text.
    """
    store = engine.store
    now = now or time.time()
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
    live = [m for m in store.list_memories(flt, limit=5000)
            if m.mtype in (MemoryType.EPISODIC, MemoryType.SEMANTIC)
            and m.metadata.get("provenance", {}).get("source") != "dream_inference"]
    report: dict = {"workspace_id": workspace_id, "repo_id": repo_id, "dry_run": dry_run,
                    "entities_considered": 0, "links_created": [], "skipped_existing": 0}
    if len(live) < 2:
        return report

    clusters = _cluster_by_subject(live, threshold=subject_jaccard)
    cluster_of: dict[str, int] = {}
    subjects: list[set] = []
    for ci, cl in enumerate(clusters):
        subjects.append(set(_common_tokens(cl, k=8)) if len(cl) > 1
                        else tokenize(f"{cl[0].title} {cl[0].content}"))
        for m in cl:
            cluster_of[m.id] = ci

    # Precompute each live memory's searchable text once (the entity loop no longer
    # rebuilds an f-string per (entity × memory) — the hot path of this sweep).
    live_text = [(m, f"{m.title} {m.content}".lower()) for m in live]
    for ent in store.list_entities(flt, limit=2000):
        name = (ent.name or "").strip()
        if len(name) < PROFILE_MIN_NAME_LEN:
            continue
        # Word-boundary match so "Redis" doesn't fire on "rediscovered": the bridging
        # entity has to appear as a *whole* token, not a substring inside an unrelated
        # word. Cheaper and more precise than the old ``needle in text`` substring scan.
        pat = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
        mentions = [m for m, text in live_text if pat.search(text)]
        cis = sorted({cluster_of[m.id] for m in mentions if m.id in cluster_of})
        if len(cis) < INFER_MIN_CLUSTERS:
            continue
        # Only genuinely non-obvious: every bridged pair must be *dissimilar* in subject.
        # A similar pair is a missed same-subject merge — the distill pass's job, not this.
        if not all(jaccard(subjects[a], subjects[b]) < subject_jaccard
                   for i, a in enumerate(cis) for b in cis[i + 1:]):
            continue
        report["entities_considered"] += 1
        reps = _cluster_reps(mentions, cluster_of, cis)
        if any(_has_inference(store, m.id) for m in reps):
            report["skipped_existing"] += 1
            continue
        content = _build_inference_content(name, ent.ntype, reps, subjects, cis, llm=llm)
        entry = {"entity": name, "etype": ent.ntype,
                 "bridges": [", ".join(sorted(subjects[c])[:4]) for c in cis],
                 "sources": [m.id for m in reps]}
        if dry_run:
            entry["would_link"] = entry["sources"]
        else:
            entry["id"] = _write_inference(engine, name, ent.ntype, reps,
                                           content=content, now=now)
        report["links_created"].append(entry)
        if len(report["links_created"]) >= max_links:
            break
    return report


def _cluster_reps(mentions: list[MemoryRecord], cluster_of: dict, cis: list[int]) -> list[MemoryRecord]:
    """One representative memory per bridged cluster (first mention seen in each)."""
    reps: list[MemoryRecord] = []
    seen: set[int] = set()
    for m in mentions:
        ci = cluster_of.get(m.id)
        if ci in cis and ci not in seen:
            reps.append(m)
            seen.add(ci)
    return reps


def _has_inference(store, memory_id: str) -> bool:
    return any(link["relation"] == INFER_RELATION for link in store.get_links(memory_id))


def _build_inference_content(name: str, etype: str, reps: list[MemoryRecord],
                             subjects: list[set], cis: list[int], *, llm: Any) -> str:
    label = f"{name} ({etype})" if etype else name
    topics = "; ".join(f"'{', '.join(sorted(subjects[c])[:4]) or 'a topic'}'" for c in cis)
    quotes = [m.content.strip().replace("\n", " ")[:200] for m in reps]
    content = (f"Possible connection via {label}: it links {topics}.\n"
               + "\n".join(f"- {q}" for q in quotes))
    if llm is not None:
        summary = _llm_summary(
            llm, _INFER_SYSTEM_PROMPT,
            f"Shared entity: {name}\nConnected notes:\n"
            + "\n".join(f"- {m.content.strip()}" for m in reps))
        if summary:
            content = f"{summary}\n\n(Inferred connection via {label}, from {len(reps)} notes)"
    return content


def _write_inference(engine, name: str, etype: str, reps: list[MemoryRecord],
                   *, content: str, now: float) -> str:
    first = reps[0]
    inference_id = engine.remember(
        content,
        workspace_id=first.workspace_id, repo_id=first.repo_id,
        mtype=MemoryType.SEMANTIC, scope=Scope(first.scope),
        title=f"Inferred connection: {name}"[:200], importance=INFER_IMPORTANCE,
        keywords=[name],
        metadata={"provenance": {"source": "dream_inference", "entity": name,
                                 "etype": etype, "trusted": False,
                                 "links": [m.id for m in reps]}},
        resolve_conflicts=False,   # an inference is new by construction
    )
    for m in reps:
        engine.store.add_link(inference_id, m.id, INFER_RELATION)
    engine.store.audit("consolidation", "infer", inference_id,
                       f"inferred a connection via {name} from {len(reps)} notes")
    return inference_id
