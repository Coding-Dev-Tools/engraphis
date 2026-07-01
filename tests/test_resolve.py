from engraphis.core.interfaces import MemoryRecord
from engraphis.core.resolve import ResolutionOp, resolve
from engraphis.core.textutil import jaccard, text_overlap, tokenize


def _rec(content, title="", id="mem_x"):
    return MemoryRecord(id=id, content=content, title=title)


def test_tokenize_drops_stopwords_and_short_tokens():
    toks = tokenize("The default branch for all repositories is called master.")
    assert "default" in toks and "branch" in toks and "master" in toks
    assert "the" not in toks and "for" not in toks and "is" not in toks


def test_jaccard_empty_is_zero():
    assert jaccard(set(), {"x"}) == 0.0
    assert jaccard(set(), set()) == 0.0


def test_text_overlap_identical_is_one():
    assert text_overlap("same words here", "same words here") == 1.0


def test_resolve_add_when_no_neighbors():
    res = resolve("We use pnpm for frontend repos.", [])
    assert res.op == ResolutionOp.ADD


def test_resolve_add_when_neighbor_below_similarity_floor():
    neighbor = _rec("Completely unrelated note about office plants.")
    res = resolve("We use pnpm for frontend repos.", [(0.05, neighbor)])
    assert res.op == ResolutionOp.ADD


def test_resolve_noop_on_near_duplicate_restatement():
    neighbor = _rec("We standardized on pnpm as the package manager for frontend repos.",
                    id="mem_old")
    res = resolve("We standardized on pnpm as the package manager for frontend repos.",
                  [(0.9, neighbor)])
    assert res.op == ResolutionOp.NOOP
    assert res.target_id == "mem_old"


def test_resolve_invalidate_on_same_subject_new_content():
    # Mirrors the rate-limit fixture: same subject, materially different value.
    neighbor = _rec("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                    id="mem_old_limit")
    candidate = "As of 2026-02 the rate limit was raised to 500 requests per minute per API key."
    res = resolve(candidate, [(0.5, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_limit"


def test_resolve_add_when_related_but_distinct_topic():
    # Cause vs. fix: related (both about the checkout race condition) but complementary,
    # not contradictory — both should be kept.
    neighbor = _rec("The bug in checkout was caused by a race condition in the inventory service.",
                    id="mem_cause")
    candidate = "We fixed the checkout race condition by adding a Redis lock around the stock decrement."
    res = resolve(candidate, [(0.4, neighbor)])
    assert res.op == ResolutionOp.ADD


def test_resolve_picks_best_overlap_among_multiple_neighbors():
    unrelated = _rec("Customer ACME is on the enterprise plan.", id="mem_acme")
    same_subject = _rec("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                        id="mem_limit")
    candidate = "As of 2026-02 the rate limit was raised to 500 requests per minute per API key."
    res = resolve(candidate, [(0.3, unrelated), (0.5, same_subject)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_limit"
