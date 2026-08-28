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


def test_keyed_duplicate_ignores_harmless_punctuation():
    neighbor = MemoryRecord(
        id="mem_punctuated",
        content="The API timeout is 30 seconds.",
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    res = resolve(
        "The API timeout is 30 seconds!",
        [(0.99, neighbor)],
        subject_key="api-timeout",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.NOOP
    assert res.target_id == "mem_punctuated"


def test_keyed_duplicate_preserves_semantic_punctuation():
    neighbor = MemoryRecord(
        id="mem_versioned",
        content="The API version is v1.2.",
        subject_key="api-version",
        claim_kind="configured_value",
    )
    res = resolve(
        "The API version is v12.",
        [(0.99, neighbor)],
        subject_key="api-version",
        claim_kind="configured_value",
    )
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_versioned"


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


# ── reworded corrections without a claim key (benchmark cc0825) ────────────────

def test_reworded_number_correction_invalidates_without_claim_key():
    # Same fact, changed value, reworded prose: the aligned diff finds the
    # 30 -> 90 swap anchored by the shared "timeout/seconds" attribute.
    neighbor = _rec("The request timeout is 30 seconds.", id="mem_old_timeout")
    res = resolve("We raised the request timeout to 90 seconds last sprint.",
                  [(0.5, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_timeout"


def test_reworded_marker_correction_invalidates_across_phrasings():
    # A bare change marker ("moved", "grew") is not sufficient on its own
    # — common words leak into every sentence. The marker leg now requires
    # the candidate to also exhibit a value_swap on the same shared
    # subject, so the rewrite is "same fact, new value" rather than a
    # different fact about a similar topic.
    neighbor = _rec("Deploy schedule runs at 5pm on Fridays.", id="mem_deploy_slot")
    res = resolve("Deploy schedule now runs at 6pm on Fridays.", [(0.6, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_deploy_slot"

    neighbor2 = _rec("The pilot cohort has 25 users.", id="mem_pilot")
    res2 = resolve("The pilot cohort has 120 users.", [(0.5, neighbor2)])
    # No marker, but a clear value swap on the same subject.
    assert res2.op == ResolutionOp.INVALIDATE
    assert res2.target_id == "mem_pilot"


def test_reworded_marker_without_value_swap_invalidates():
    """Under the attribute-correction contract (see P1 review on PR #181),
    a single nonnumeric noun-for-noun swap with at least 2 shared subject
    tokens invalidates regardless of marker or predicate. The candidate
    and neighbour disagree on the API's backing infrastructure (Redis
    caching for user sessions vs three replicas for high availability);
    the resolver treats the candidate as the newer fact and supersedes
    the neighbour. The marker leg alone is intentionally not enough —
    attribute_corrected covers the case where a marker accompanies a
    single-attribute restatement.
    """
    neighbor = _rec("The production API uses Redis caching for user sessions.",
                    id="mem_cache")
    res = resolve("The production API now uses three replicas for high availability.",
                  [(0.45, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_cache"


def test_date_swap_correction_invalidates_when_attribute_is_shared():
    neighbor = _rec("Until January the rate limit was 100 requests per minute.",
                    id="mem_old_rate")
    res = resolve("As of February the rate limit is 500 requests per minute.",
                  [(0.55, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_old_rate"


def test_distinct_environment_values_never_invalidate_each_other():
    # Same attribute, different environment qualifier: two coexisting facts.
    neighbor = _rec("Redis cache TTL is 300 seconds in staging.", id="mem_ttl_staging")
    res = resolve("Redis cache TTL is 3600 seconds in production.", [(0.7, neighbor)])
    assert res.op != ResolutionOp.INVALIDATE


def test_named_identifier_swap_is_not_proven_a_correction_without_marker():
    # ProviderA -> ProviderB beside 4 -> 8 workers could be parallel infrastructure;
    # the hashing embedder cannot prove the same predicate, so both stay live.
    neighbor = _rec("CI runs on ProviderA with 4 workers.", id="mem_ci_workers")
    res = resolve("CI runs on ProviderB with 8 workers.", [(0.65, neighbor)])
    assert res.op != ResolutionOp.INVALIDATE


def test_single_noun_swap_on_tight_subject_invalidates():
    """A single nonnumeric noun-for-noun swap on a tight shared subject
    ("the docs cover the REST interface" -> "...the GraphQL interface")
    is a correction of the same attribute, not two coexisting facts.
    The surrounding "the docs cover the" matches on both sides, so the
    attribute anchor holds and the candidate supersedes the neighbour.
    """
    neighbor = _rec("The docs cover the REST interface.", id="mem_docs_rest")
    res = resolve("The docs cover the GraphQL interface.", [(0.9, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE


def test_default_branch_master_to_main_invalidates():
    """rc06 in eval/datasets/resolver_reworded_corrections.jsonl. Single
    noun-for-noun swap on a tight shared subject is a correction under
    the attribute-correction contract.
    """
    neighbor = _rec("The default branch is named master.", id="mem_branch_master")
    res = resolve("The default branch is named main.", [(0.85, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_branch_master"


def test_default_admin_root_to_admin_invalidates():
    """rc10 in eval/datasets/resolver_reworded_corrections.jsonl."""
    neighbor = _rec("The default admin user is root.", id="mem_admin_root")
    res = resolve("The default admin user is now admin.", [(0.85, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_admin_root"


def test_log_level_info_to_debug_invalidates():
    """rc33 in eval/datasets/resolver_reworded_corrections.jsonl."""
    neighbor = _rec("Default log level is INFO.", id="mem_loglevel_info")
    res = resolve("Default log level is DEBUG now.", [(0.85, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_loglevel_info"


def test_finding_one_about_caching_and_finding_two_about_latency_invalidates():
    """Under the attribute-correction contract, the +/- 2 anchor check
    on "Finding ... about" sees "about"/"finding" as shared attribute
    context, so the single heavy-noun swap (caching vs latency) is a
    correction. The engine layer still creates a shared-source edge on
    the retained memory; the resolver's contract is per-pair.
    """
    neighbor = _rec("Finding one about caching.", id="mem_a")
    res = resolve("Finding two about latency budgets.", [(0.9, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_a"


def test_distinct_attribute_with_numbers_stays_live():
    # "refreshes every 5 minutes" vs "holds about 2 million documents": numbers
    # change but they belong to different attributes (no shared value anchor).
    neighbor = _rec("The search index refreshes every 5 minutes.", id="mem_index_refresh")
    res = resolve("The search index holds about 2 million documents.", [(0.6, neighbor)])
    assert res.op != ResolutionOp.INVALIDATE


def test_engine_reworded_correction_closes_the_stale_fact_end_to_end():
    from engraphis.core.engine import MemoryEngine
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        wid = eng.store.get_or_create_workspace("w")
        rid = eng.store.get_or_create_repo(wid, "r")
        old = eng.remember_with_resolution(
            "The request timeout is 30 seconds.", workspace_id=wid, repo_id=rid)
        new = eng.remember_with_resolution(
            "We raised the request timeout to 90 seconds last sprint.",
            workspace_id=wid, repo_id=rid)
        assert new["op"] == "invalidate"
        assert new["superseded"] == [old["id"]]
        assert eng.store.get_memory(old["id"]).valid_to is not None
        assert eng.store.get_memory(new["id"]).valid_to is None
    finally:
        eng.store.close()


def test_engine_distinct_facts_about_one_topic_both_stay_live():
    from engraphis.core.engine import MemoryEngine
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        wid = eng.store.get_or_create_workspace("w")
        rid = eng.store.get_or_create_repo(wid, "r")
        budget = eng.remember_with_resolution(
            "The data migration budget is 50 thousand dollars.",
            workspace_id=wid, repo_id=rid)
        deadline = eng.remember_with_resolution(
            "The data migration deadline is March 15.",
            workspace_id=wid, repo_id=rid)
        assert deadline["op"] in ("add", "relate")
        assert eng.store.get_memory(budget["id"]).valid_to is None
        assert eng.store.get_memory(deadline["id"]).valid_to is None
    finally:
        eng.store.close()


# ── R1 review: additional safety-class tests for reworded-correction paths ──────


def test_distinct_environment_values_never_invalidate_via_strong_branch_long_form():
    # The short-form env-conflict test (Redis TTL 300 -> 3600 in staging/production)
    # happened to split into two diff spans; this long-form variant collapses to a
    # single replace span. R1 review found the strong branch's swap_veto did not
    # honour env_conflict, so the strong path would incorrectly supersede.
    neighbor = _rec(
        "The primary database connection pool in the staging environment holds "
        "5 connections per application instance under nominal load.",
        id="mem_staging_pool",
    )
    res = resolve(
        "The primary database connection pool in the production environment holds "
        "8 connections per application instance under nominal load.",
        [(0.7, neighbor)],
    )
    assert res.op != ResolutionOp.INVALIDATE


def test_marker_with_value_swap_invalidates():
    # The marker leg now requires a value_swap on the same shared subject
    # — marker + value is a real correction ("now runs 6pm" rewrites
    # "runs 5pm"). A bare marker without a value change stays ADD.
    neighbor = _rec("Deploy schedule runs at 5pm on Fridays.", id="mem_deploy_v")
    res = resolve("Deploy schedule now runs at 6pm on Fridays.",
                  [(0.6, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_deploy_v"


def test_marker_alone_without_value_swap_invalidates():
    # Under the attribute-correction contract (see P1 review on PR #181),
    # a single nonnumeric noun-for-noun swap with at least 2 shared
    # subject tokens invalidates even without a value_swap. The marker
    # ("We migrated ...") accompanies the single heavy swap (REST ->
    # GraphQL) and the candidate now says the docs cover the GraphQL
    # interface in place of REST. The neighbour is superseded.
    neighbor = _rec("The docs cover the REST interface.", id="mem_docs_rest")
    res = resolve("We migrated the docs to cover the GraphQL interface.",
                  [(0.9, neighbor)])
    assert res.op == ResolutionOp.INVALIDATE


def test_closed_predecessor_supersedes_under_strong_evidence_regardless_of_prose():
    # A closed (valid_to set) predecessor is historical chain membership: even
    # the strong path's swap_vetoes should not protect a closed record. This
    # is the backfill-and-supersede contract: "the chain wants this rewrite".
    closed = _memory_obj(
        id="mem_closed_old",
        content="Deploy schedule runs at 5pm on Fridays.",
        valid_to=1_000.0,
    )
    res = resolve("Deploy schedule now runs at 6pm on Fridays.",
                  [(0.9, closed)])
    assert res.op == ResolutionOp.INVALIDATE
    assert res.target_id == "mem_closed_old"


def _memory_obj(id: str, content: str, *, valid_to=None):
    """Minimal in-memory MemoryRecord with the fields _rec doesn't expose."""
    from engraphis.core.interfaces import MemoryRecord
    return MemoryRecord(
        id=id, workspace_id="w", repo_id=None, session_id=None,
        title="", content=content, mtype="semantic", scope="workspace",
        importance=0.0, confidence=1.0, valid_from=0.0, valid_to=valid_to,
        ingested_at=0.0, expired_at=None,
        subject_key="", claim_kind="", keywords=(), metadata={},
        provenance={"source": "agent", "trusted": True, "review_state": "approved"},
    )
