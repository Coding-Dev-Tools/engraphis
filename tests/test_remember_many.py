"""Batch assembly: ``MemoryEngine.remember_many`` and the service/MCP wrappers.

Implements the fan-out → collect → wire lifecycle for parallel-agent output: all
facts are embedded in one call, each fact is resolved against existing memory AND
its already-resolved batch siblings inside one transaction, and afterwards batch
siblings that share a non-empty ``subject_key`` or ``provenance.source`` are wired
with evidence-labeled ``related`` edges ("no shared source, no edge" — similarity
alone never creates a sibling edge).
"""
import pytest
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import FactSpec, MemoryType


def _engine():
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    return eng, wid, rid


def _live_links(eng, mid):
    rows = eng.store.conn.execute(
        "SELECT relation, a, b, reason FROM mem_links "
        "WHERE (a=? OR b=?) AND valid_to IS NULL AND expired_at IS NULL",
        (mid, mid),
    ).fetchall()
    return [dict(row) for row in rows]


def test_batch_returns_one_result_per_fact_in_order():
    eng, wid, rid = _engine()
    results = eng.remember_many(
        ["alpha fact", "beta fact", "gamma fact"],
        workspace_id=wid, repo_id=rid,
    )
    assert len(results) == 3
    assert [r["op"] for r in results] == ["add", "add", "add"]
    assert all(isinstance(r.get("id"), str) and r["id"] for r in results)


def test_within_batch_duplicate_reinforces_instead_of_duplicating():
    """A near-exact restatement later in the same batch resolves NOOP against the
    sibling inserted earlier in the same transaction."""
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            "The deploy script runs migrations before restarting workers.",
            "The deploy script runs migrations before restarting workers.",
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert results[0]["op"] == "add"
    assert results[1]["op"] == "noop"
    assert results[1]["id"] == results[0]["id"]


def test_shared_subject_key_supersedes_within_batch():
    """A keyed claim later in the batch supersedes its same-key sibling inserted
    earlier — claim identity wins inside the batch exactly as across writes."""
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            FactSpec(content="Rate limit is 60 rpm.", subject_key="api.rate_limit"),
            FactSpec(content="Rate limit is 120 rpm.", subject_key="api.rate_limit"),
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert results[0]["op"] == "add"
    assert results[1]["op"] == "invalidate"
    assert results[0]["id"] in results[1]["superseded"]


def test_cross_type_sibling_does_not_drive_resolution():
    """Sibling candidates obey the same memory-type filter as vector search: an
    identical restatement of a *different* type must not dedupe against its
    cross-type sibling, while the same-type restatement still does."""
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            FactSpec(
                content="The deploy script runs migrations before restarting workers.",
                mtype=MemoryType.SEMANTIC,
            ),
            FactSpec(
                content="The deploy script runs migrations before restarting workers.",
                mtype=MemoryType.EPISODIC,
            ),
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert results[0]["op"] == "add"
    assert results[1]["op"] == "add"
    assert results[1]["id"] != results[0]["id"]
    # Positive control: the same restatement within one type resolves NOOP.
    again = eng.remember_many(
        [FactSpec(
            content="The deploy script runs migrations before restarting workers.",
            mtype=MemoryType.EPISODIC,
        )],
        workspace_id=wid, repo_id=rid,
    )
    assert again[0]["op"] == "noop"
    assert again[0]["id"] == results[1]["id"]


def test_shared_provenance_source_creates_evidence_edge():
    # Under the attribute-correction contract, a single heavy-noun swap
    # with a tight shared subject invalidates the previous fact, so the
    # second sibling no longer survives as ADD. The engine's
    # remember_many path still creates the shared-source edge on the
    # retained memory; the contract under test is the edge, not the
    # resolver op. Use two genuinely distinct facts so the resolver
    # leaves them both live.
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            FactSpec(content="The cache is keyed on URL.", evidence_source="subagent-7"),
            FactSpec(content="Sessions are persisted to disk every 30 seconds.",
                     evidence_source="subagent-7"),
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert all(r["op"] == "add" for r in results)
    ids = {r["id"] for r in results}
    edges = []
    for mid in ids:
        edges.extend(
            link for link in _live_links(eng, mid) if link["relation"] == "related"
        )
    assert edges, "expected a shared-source edge between siblings"
    assert any("subagent-7" in (link["reason"] or "") for link in edges)


def test_engine_default_provenance_is_not_evidence():
    """Two facts with no declared source stay unwired even though the engine
    stamps identical default provenance on both — defaults are not evidence."""
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            FactSpec(content="Kubernetes pods restart on failure."),
            FactSpec(content="The recipe needs two cups of flour."),
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert all(r["op"] == "add" for r in results)
    for r in results:
        related = [
            link for link in _live_links(eng, r["id"])
            if link["relation"] == "related"
        ]
        assert not related


def test_different_declared_sources_do_not_wire():
    eng, wid, rid = _engine()
    results = eng.remember_many(
        [
            FactSpec(content="Caching finding.", evidence_source="subagent-1"),
            FactSpec(content="Latency finding.", evidence_source="subagent-2"),
        ],
        workspace_id=wid, repo_id=rid,
    )
    assert all(r["op"] == "add" for r in results)
    for r in results:
        related = [
            link for link in _live_links(eng, r["id"])
            if link["relation"] == "related"
        ]
        assert not related


def test_batch_rolls_back_atomically_on_failure():
    """An invalid fact anywhere in the batch aborts the whole transaction: nothing
    from the batch is visible afterwards."""
    eng, wid, rid = _engine()
    try:
        eng.remember_many(
            [
                "good fact one",
                "good fact two",
                None,  # type: ignore[list-item]
            ],
            workspace_id=wid, repo_id=rid,
        )
    except TypeError:
        pass
    else:
        raise AssertionError("expected a TypeError for the non-string fact")
    remaining = eng.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE workspace_id=?", (wid,)
    ).fetchone()["n"]
    assert remaining == 0


def test_empty_batch_is_a_noop():
    eng, wid, rid = _engine()
    assert eng.remember_many([], workspace_id=wid, repo_id=rid) == []


def test_batch_exceeding_cap_raises():
    eng, wid, rid = _engine()
    try:
        eng.remember_many(
            [f"fact {i}" for i in range(501)], workspace_id=wid, repo_id=rid,
        )
    except ValueError as exc:
        assert "MAX_FACTS_PER_BATCH" in str(exc)
    else:
        raise AssertionError("expected ValueError above the batch cap")


def test_service_remember_many_wires_and_reports_ops():
    from engraphis.service import MemoryService

    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    svc = MemoryService(eng)
    out = svc.remember_many(
        [
            {"content": "Auth uses JWT.", "subject_key": "auth.token",
             "evidence_source": "subagent-3"},
            {"content": "JWTs are rotated hourly.", "subject_key": "auth.rotation",
             "evidence_source": "subagent-3"},
        ],
        workspace="w",
    )
    assert out["stored"] is True
    assert out["total"] == 2
    assert out["ops"] == ["add", "add"]
    assert all(r["id"] for r in out["results"])


def test_service_remember_many_supersedes_keyed_claims():
    from engraphis.service import MemoryService

    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    svc = MemoryService(eng)
    out = svc.remember_many(
        [
            {"content": "Rate limit is 60 rpm.", "subject_key": "api.rate_limit"},
            {"content": "Rate limit is 120 rpm.", "subject_key": "api.rate_limit"},
        ],
        workspace="w",
    )
    assert out["ops"] == ["add", "invalidate"]


def test_untrusted_batch_cannot_supersede_and_stays_pending():
    """An external-source batch is untrusted end-to-end: like single untrusted
    writes it cannot resolve against or invalidate live memory, and every stored
    record keeps pending, trusted=False provenance rather than minting trusted
    graph state."""
    from engraphis.service import MemoryService

    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    svc = MemoryService(eng)
    out = svc.remember_many(
        [
            {"content": "Rate limit is 60 rpm.", "subject_key": "api.rate_limit"},
            {"content": "Rate limit is 120 rpm.", "subject_key": "api.rate_limit"},
        ],
        workspace="w", source="api",
    )
    assert out["ops"] == ["add", "add"]  # no supersession despite shared subject_key
    assert len({r["id"] for r in out["results"]}) == 2
    for r in out["results"]:
        rec = eng.store.get_memory(r["id"])
        prov = rec.metadata["provenance"]
        assert prov["trusted"] is False
        assert prov["review_state"] == "pending"


def test_mcp_tool_registered():
    import asyncio

    pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")
    import engraphis.mcp_server as mcp_server

    tools = {
        t.name for t in asyncio.run(mcp_server.classic_mcp.list_tools())
    }
    assert "engraphis_remember_many" in tools


class _PublicationSpyIndex:
    """Delegates search to the engine's canonical index but records the explicit
    post-commit publications a separately-backed index (e.g. sqlite-vec) receives."""

    def __init__(self, inner):
        self._inner = inner
        self.published: list[str] = []

    def search(self, vec, k, *, filter=None):
        return self._inner.search(vec, k, filter=filter)

    def upsert(self, ids, _vecs, meta=None, *, commit=True):
        self.published.extend(ids)

    def delete(self, ids, *, commit=True):
        pass


def test_noop_resolved_fact_does_not_publish_vector_for_existing_memory():
    """A batch fact resolving NOOP against a pre-existing memory must leave that
    memory's published vector untouched: only newly inserted records are queued
    for external-index publication, otherwise the candidate vector would
    overwrite the stored record's vector although its content never changed."""
    eng, wid, rid = _engine()
    spy = _PublicationSpyIndex(eng.index)
    eng.index = spy

    fact = FactSpec(content="Deploy cutoff is Friday 17:00 UTC.",
                    subject_key="deploy.cutoff", claim_kind="policy",
                    valid_from=1_000_000.0)
    first = eng.remember_many([fact], workspace_id=wid, repo_id=rid)
    assert first[0]["op"] == "add"
    existing_id = first[0]["id"]
    assert spy.published == [existing_id]

    second = eng.remember_many(
        [FactSpec(content=fact.content, subject_key=fact.subject_key,
                  claim_kind=fact.claim_kind, valid_from=fact.valid_from)],
        workspace_id=wid, repo_id=rid,
    )
    assert second[0]["op"] == "noop"
    assert second[0]["id"] == existing_id
    assert spy.published == [existing_id]


def test_batch_publishes_each_newly_inserted_record_exactly_once():
    """Inserted siblings publish once each; a sibling that resolves NOOP against
    an earlier insert adds no second publication for the same memory id."""
    eng, wid, rid = _engine()
    spy = _PublicationSpyIndex(eng.index)
    eng.index = spy

    results = eng.remember_many(
        ["alpha fact", "alpha fact", "distinct beta fact"],
        workspace_id=wid, repo_id=rid,
    )
    assert results[0]["op"] == "add"
    assert results[1]["op"] == "noop"
    assert results[2]["op"] == "add"
    assert sorted(spy.published) == sorted({results[0]["id"], results[2]["id"]})


def test_session_ended_between_validation_and_transaction_rejects_batch():
    """A session closed after the early ownership check but before the batch
    transaction fails cleanly instead of attaching its writes to an ended
    session: the in-transaction liveness revalidation rejects the whole batch."""
    eng, wid, rid = _engine()
    sid = eng.store.start_session(wid, rid)

    real_get_session = eng.store.get_session

    def get_session_then_end(session_id):
        session = real_get_session(session_id)
        eng.store.end_session(session_id)
        return session

    eng.store.get_session = get_session_then_end
    try:
        with pytest.raises(ValueError, match="not active"):
            eng.remember_many(
                ["late fact for a closing session"],
                workspace_id=wid, repo_id=rid, session_id=sid,
            )
    finally:
        eng.store.get_session = real_get_session

    rows = eng.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE session_id=?", (sid,)
    ).fetchone()
    assert rows["n"] == 0
