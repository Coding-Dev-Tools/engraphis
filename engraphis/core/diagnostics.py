"""Bounded, content-free recall observations. Unobserved exclusions stay unknown."""
from __future__ import annotations

import math
from typing import Optional


def recall_diagnostics(result, *, elapsed_ms: float,
                       phase_ms: Optional[dict[str, float]] = None) -> dict:
    usage = result.usage
    raw = getattr(usage, "omission_reasons", {}) or {}
    counts: dict[str, Optional[int]] = {
        name: min(1_000_000_000, max(0, int(raw.get(name, 0))))
        for name in ("duplicate", "budget", "score_tail", "missing_record")
    }
    counts["truncated"] = sum(bool(chunk.truncated) for chunk in result.packed_chunks)
    # Candidate discovery is bounded. Counting all invisible records would both
    # expand the scan and disclose facts about data outside the authorized scope.
    counts.update({"scope": None, "time": None, "trust": None, "supersession": None})
    elapsed = max(0.0, elapsed_ms) if math.isfinite(elapsed_ms) else 0.0
    phases = {"engine_recall": round(elapsed, 3)}
    # Only fixed phase names cross this content-free diagnostic boundary.
    for name in ("preparation", "planning", "embedding", "candidate_filtering",
                 "vector_search", "lexical_search", "graph_search", "code_search",
                 "fusion_scoring", "reranking", "selection", "reinforcement",
                 "support_and_provenance", "packing", "response_metadata"):
        value = (phase_ms or {}).get(name)
        if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0:
            phases[name] = round(value, 3)
    return {
        "schema": "diagnostics/1", "counts": counts,
        "count_boundary": "packing input; null means not observed",
        "phase_ms": phases,
        "phase_boundary": "disjoint engine wall time; repeated arms accumulate; engine_recall is the enclosing total",
        "timing_boundary": "engine entry through packing; excludes transport queue and answer generation",
        "index": {
            "ready": bool(result.vector_search_ready),
            "degraded": bool(result.degraded_mode),
            "repair_pending": result.vector_index_repairs_pending,
            "source": result.vector_search_source if result.vector_search_source in
                      {"configured", "canonical", "canonical_fallback", "disabled"} else "other",
        },
    }
