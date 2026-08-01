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


def test_resolve_claim_key_invalidates_without_lexical_overlap():
    neighbor = MemoryRecord(
        id="mem_old_limit", content="The upstream provider permits 100 calls.",
        subject_key="provider-rate-limit", claim_kind="limit",
    )
    res = resolve(
        "The current cap is 500 requests per minute.", [(0.2, neighbor)],
        subject_key="provider-rate-limit", claim_kind="limit",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_limit"


def test_resolve_never_deduplicates_or_invalidates_conflicting_claim_keys():
    neighbor = MemoryRecord(
        id="mem_database_status",
        content="The status is enabled.",
        subject_key="database",
        claim_kind="status",
    )
    res = resolve(
        "The status is enabled.",
        [(0.99, neighbor)],
        subject_key="billing",
        claim_kind="status",
    )
    assert res.op == ResolutionOp.ADD


def test_resolve_requires_claim_kind_equality_for_keyed_invalidation():
    neighbor = MemoryRecord(
        id="mem_deploy_owner",
        content="Production deploys use the platform team.",
        subject_key="production-deploy",
        claim_kind="owner",
    )
    res = resolve(
        "Production deploys use the release train.",
        [(0.99, neighbor)],
        subject_key="production-deploy",
        claim_kind="process",
    )
    assert res.op == ResolutionOp.ADD


def test_shared_claim_key_invalidates_even_when_only_a_number_changes():
    neighbor = MemoryRecord(
        id="mem_old_timeout",
        content="The request timeout is 5 seconds.",
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    res = resolve(
        "The request timeout is 30 seconds.",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_timeout"


def test_exact_claim_identity_outranks_a_more_similar_unkeyed_neighbor():
    keyed = MemoryRecord(
        id="mem_keyed",
        content="The cap is one hundred.",
        subject_key="provider-cap",
        claim_kind="limit",
    )
    unkeyed = MemoryRecord(
        id="mem_unkeyed",
        content="The current cap is five hundred.",
    )
    res = resolve(
        "The current cap is five hundred.",
        [(0.2, keyed), (0.999, unkeyed)],
        subject_key="provider-cap",
        claim_kind="limit",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_keyed"


def test_keyed_duplicate_ignores_existing_display_title():
    neighbor = MemoryRecord(
        id="mem_titled",
        title="API policy",
        content="The timeout is 30 seconds.",
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    res = resolve(
        "The timeout is 30 seconds.",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.NOOP
    assert res.target_id == "mem_titled"


def test_keyed_duplicate_with_matching_display_title_compares_content_only():
    neighbor = MemoryRecord(
        id="mem_titled",
        title="API policy",
        content="The timeout is 30 seconds.",
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    res = resolve(
        "API policy\nThe timeout is 30 seconds.",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
        candidate_content="The timeout is 30 seconds.",
    )
    assert res.op == ResolutionOp.NOOP
    assert res.target_id == "mem_titled"


def test_new_claim_identity_replaces_instead_of_nooping_unkeyed_duplicate():
    neighbor = MemoryRecord(
        id="mem_unkeyed_duplicate",
        content="The timeout is 30 seconds.",
    )
    res = resolve(
        "The timeout is 30 seconds.",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_unkeyed_duplicate"


def test_new_claim_identity_preserves_a_reworded_unkeyed_memory():
    neighbor = MemoryRecord(
        id="mem_unkeyed",
        content="The API timeout is 30 seconds.",
    )
    res = resolve(
        "The API timeout is 30 seconds!",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.RELATE
    assert res.target_id == "mem_unkeyed"


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


# ── explicit claim identity is the low-overlap resolution contract ─────────────

def test_low_similarity_unkeyed_rewrite_remains_distinct_without_claim_identity():
    neighbor = _rec("The API rate limit is one hundred requests every sixty seconds.",
                    id="mem_old_phrasing")
    res = resolve("Calls are capped at 500 per minute for each key.", [(0.01, neighbor)])
    assert res.op == ResolutionOp.ADD


def test_low_similarity_rewrite_supersedes_with_shared_claim_identity():
    old = MemoryRecord(
        id="mem_old_limit",
        content="The API rate limit is one hundred requests every sixty seconds.",
        subject_key="api-rate-limit",
        claim_kind="configured_value",
    )
    new = "Calls are capped at 500 per minute for each key."
    res = resolve(
        new, [(0.01, old)],
        subject_key="api-rate-limit", claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_limit"


def test_resolve_exact_restatement_still_noops_despite_high_cosine():
    text = "We standardized on pnpm as the package manager for frontend repos."
    res = resolve(text, [(0.97, _rec(text, id="mem_dup"))])
    assert res.op == ResolutionOp.NOOP           # the duplicate rule fires first


def test_resolve_moderate_cosine_low_overlap_still_adds():
    # Related-but-complementary stays ADD without claim identity or enough
    # lexical evidence, regardless of a candidate-discovery cosine.
    neighbor = _rec("The bug in checkout was caused by a race condition in the inventory "
                    "service.", id="mem_cause")
    candidate = ("We fixed the checkout race condition by adding a Redis lock around the "
                 "stock decrement.")
    res = resolve(candidate, [(0.6, neighbor)])
    assert res.op == ResolutionOp.ADD
