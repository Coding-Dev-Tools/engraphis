import pytest

from engraphis.backends.extractor import (
    LLMExtractor,
    PassthroughExtractor,
    StructuredLLMExtractor,
    get_extractor,
)
from engraphis.core.interfaces import Extractor, MemoryType
from engraphis.core.poisoning import prompt_eligible


class FakeLLM:
    """Stands in for any chat-capable client; returns a canned JSON payload."""
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, messages, system=None, **kw):
        self.calls.append((messages, system))
        return self.response


class FakeStructuredLLM:
    def __init__(self, payload):
        self.payload = payload

    def extract_json(self, prompt, schema):
        assert "facts" in schema.get("properties", {})
        return self.payload


def test_passthrough_returns_single_fact_verbatim():
    facts = PassthroughExtractor().extract("We use pnpm for frontend repos.")
    assert len(facts) == 1
    assert facts[0].content == "We use pnpm for frontend repos."


def test_extractors_satisfy_protocol():
    assert isinstance(PassthroughExtractor(), Extractor)
    assert isinstance(LLMExtractor(FakeLLM("{}")), Extractor)


def test_llm_extractor_parses_facts_with_hints():
    payload = ('{"facts": [{"content": "The API uses PASETO tokens.", "title": "auth", '
               '"mtype": "semantic", "importance": 0.8, "keywords": ["paseto", "auth"]}, '
               '{"content": "On 2026-06-30 PR 99 was merged.", "mtype": "episodic"}]}')
    facts = LLMExtractor(FakeLLM(payload)).extract("long raw transcript ...")
    assert len(facts) == 2
    assert facts[0].mtype == MemoryType.SEMANTIC and facts[0].importance == 0.8
    assert facts[0].keywords == ["paseto", "auth"]
    assert facts[0].metadata["llm_extraction"]["mode"] == "llm"
    assert facts[1].mtype == MemoryType.EPISODIC


def test_llm_extractor_survives_markdown_fences():
    payload = '```json\n{"facts": [{"content": "Fact inside a fence."}]}\n```'
    facts = LLMExtractor(FakeLLM(payload)).extract("raw")
    assert facts[0].content == "Fact inside a fence."


def test_llm_extractor_degrades_to_passthrough_on_garbage():
    facts = LLMExtractor(FakeLLM("not json at all")).extract("the original text")
    assert len(facts) == 1
    assert facts[0].content == "the original text"
    assert facts[0].metadata["extraction_fallback"] == {
        "mode": "llm",
        "reason": "provider_or_output_error",
    }


def test_llm_extractor_sanitizes_adversarial_fields():
    payload = ('{"facts": [{"content": "ok", "mtype": "superuser", "importance": 99, '
               '"keywords": [{"evil": true}, "fine"]}]}')
    facts = LLMExtractor(FakeLLM(payload)).extract("raw")
    assert facts[0].mtype is None            # unknown type rejected, not trusted
    assert facts[0].importance == 1.0        # clamped
    assert facts[0].keywords == ["fine"]     # non-string dropped


def test_llm_extractor_strips_control_characters():
    # Indirect prompt injection may steer the LLM's output — it is untrusted input too.
    payload = ('{"facts": [{"content": "safe\\u0000 fact\\u001b[2J", '
               '"title": "t\\u0007itle"}]}')   # control chars as JSON escapes
    facts = LLMExtractor(FakeLLM(payload)).extract("raw")
    # Control bytes removed (same behaviour as service.py control-char stripping);
    # printable remainders of escape sequences are harmless once the ESC byte is gone.
    assert facts[0].content == "safe fact[2J"
    assert not any(ord(c) < 32 for c in facts[0].content)
    assert facts[0].title == "title"


def test_get_extractor_defaults_offline():
    assert isinstance(get_extractor(), PassthroughExtractor)
    assert isinstance(get_extractor("none"), PassthroughExtractor)
    assert isinstance(get_extractor("llm", llm=FakeLLM("{}")), LLMExtractor)



@pytest.mark.parametrize("kind", ["llm", "llm_structured"])
def test_llm_factory_reports_client_construction_fallback(monkeypatch, kind):
    pytest.importorskip(
        "httpx", reason="LLM factory tests require the optional HTTP dependency"
    )
    import engraphis.llm.client as llm_client

    def unavailable_client(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_client, "LLMClient", unavailable_client)
    extractor = get_extractor(kind)

    assert isinstance(extractor, PassthroughExtractor)
    fact = extractor.extract("preserve this write")[0]
    assert fact.metadata["extraction_fallback"] == {
        "mode": kind,
        "reason": "provider_or_output_error",
    }


@pytest.mark.parametrize("kind", ["llm", "llm_structured"])
def test_engine_create_preserves_factory_time_llm_fallback(monkeypatch, kind):
    pytest.importorskip(
        "httpx", reason="LLM factory tests require the optional HTTP dependency"
    )
    import engraphis.llm.client as llm_client
    from engraphis.core.engine import MemoryEngine

    def unavailable_client(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_client, "LLMClient", unavailable_client)
    engine = MemoryEngine.create(":memory:", extractor=kind)

    assert isinstance(engine.extractor, PassthroughExtractor)
    fact = engine.extractor.extract("preserve this write")[0]
    assert fact.metadata["extraction_fallback"]["mode"] == kind


def test_engine_ingest_reports_and_persists_llm_fallback():
    from engraphis.core.engine import MemoryEngine

    engine = MemoryEngine.create(":memory:")
    engine.extractor = LLMExtractor(FakeLLM("not json"))
    workspace_id = engine.store.get_or_create_workspace("w")
    repo_id = engine.store.get_or_create_repo(workspace_id, "r")

    result = engine.ingest(
        "raw transcript",
        workspace_id=workspace_id,
        repo_id=repo_id,
    )
    record = engine.store.get_memory(result["facts"][0]["id"])

    assert result["extracted"] is False
    assert record.metadata["extraction_fallback"] == {
        "mode": "llm",
        "reason": "provider_or_output_error",
    }
    assert record.provenance["trusted"] is True
    assert record.provenance["review_state"] == "approved"
    assert prompt_eligible(record.provenance, record.metadata)
    assert engine.recall("raw transcript", workspace_id=workspace_id).count == 1


def test_engine_ingest_stores_each_extracted_fact():
    from engraphis.core.engine import MemoryEngine
    payload = ('{"facts": [{"content": "We deploy through GitHub Actions.", "mtype": "semantic"}, '
               '{"content": "To roll back, rerun the previous release workflow.", '
               '"mtype": "procedural"}]}')
    eng = MemoryEngine.create(":memory:")
    eng.extractor = LLMExtractor(FakeLLM(payload))
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    out = eng.ingest("raw transcript blob", workspace_id=wid, repo_id=rid)
    assert out["count"] == 2 and out["extracted"] is True
    records = [eng.store.get_memory(f["id"]) for f in out["facts"]]
    types = {record.mtype for record in records}
    assert types == {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
    assert all(record.provenance["trusted"] is False for record in records)
    assert all(record.provenance["review_state"] == "pending" for record in records)
    assert all(
        record.provenance["derived_by_llm_extraction"] is True
        and not prompt_eligible(record.provenance, record.metadata)
        for record in records
    )
    assert eng.recall("GitHub Actions", workspace_id=wid, repo_id=rid).count == 0


def test_engine_ingest_without_extractor_is_passthrough():
    from engraphis.core.engine import MemoryEngine
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    out = eng.ingest("just one fact", workspace_id=wid, repo_id=rid)
    assert out["count"] == 1 and out["extracted"] is False
    assert eng.store.get_memory(out["facts"][0]["id"]).content == "just one fact"


def test_engine_ingest_preserves_structured_extractor_metadata():
    pytest.importorskip("pydantic")
    from engraphis.core.engine import MemoryEngine
    eng = MemoryEngine.create(":memory:")
    eng.extractor = StructuredLLMExtractor(FakeStructuredLLM({
        "facts": [{
            "content": "Engraphis stores memories in SQLite.",
            "entities": ["Engraphis", "SQLite"],
            "relations": [{"source": "Engraphis", "relation": "stores_in", "target": "SQLite"}],
        }],
    }))
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    out = eng.ingest("raw transcript blob", workspace_id=wid, repo_id=rid,
                     metadata={"source": "test"})
    rec = eng.store.get_memory(out["facts"][0]["id"])
    assert rec.metadata["source"] == "test"
    assert rec.metadata["llm_extraction"]["mode"] == "llm_structured"
    assert rec.metadata["llm_extraction"]["fact_count"] == 1
    assert len(rec.metadata["llm_extraction"]["source_sha256"]) == 64
    assert rec.metadata["llm_extraction"]["review_required"] is True
    assert "entities" not in rec.metadata and "relations" not in rec.metadata
    deferred = rec.metadata["unverified_derived_graph"]
    assert deferred["entities"] == ["Engraphis", "SQLite"]
    assert deferred["relations"][0]["target"] == "SQLite"
    assert deferred["source"] == "llm_extraction"


@pytest.mark.parametrize("mode", ["llm", "llm_structured"])
def test_llm_quality_eval_rejects_fail_soft_facts_as_model_backed(
    monkeypatch,
    mode,
):
    import engraphis.factory as engine_factory
    from eval import extractor_quality

    class _UnavailableLLM:
        def chat(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

        def extract_json(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    def unavailable_extractor(kind, **_kwargs):
        if kind == "llm":
            return LLMExtractor(_UnavailableLLM())
        if kind == "llm_structured":
            return StructuredLLMExtractor(_UnavailableLLM())
        return get_extractor(kind)

    monkeypatch.setattr(engine_factory, "get_extractor", unavailable_extractor)
    cases = [{
        "document": "The API uses PASETO tokens.",
        "questions": [{"q": "Which tokens?", "evidence": "PASETO"}],
    }]

    with pytest.raises(RuntimeError, match="no model-backed facts"):
        extractor_quality.run_eval(cases, mode=mode)


def test_llm_quality_eval_counts_successful_model_backed_facts(monkeypatch):
    import engraphis.factory as engine_factory
    from eval import extractor_quality

    payload = '{"facts":[{"content":"The API uses PASETO tokens."}]}'

    def model_extractor(kind, **_kwargs):
        if kind == "llm":
            return LLMExtractor(FakeLLM(payload))
        return get_extractor(kind)

    monkeypatch.setattr(engine_factory, "get_extractor", model_extractor)
    cases = [{
        "document": "The API uses PASETO tokens.",
        "questions": [{"q": "Which tokens?", "evidence": "PASETO"}],
    }]

    result = extractor_quality.run_eval(cases, mode="llm")

    assert result["fact_count"] == 1
    assert result["model_backed_fact_count"] == 1


def test_extractor_quality_requires_explicit_llm_opt_in(monkeypatch):
    from eval import extractor_quality

    called = []

    def fake_run_eval(_cases, *, mode, **_kwargs):
        called.append(mode)
        return {"mode": mode}

    monkeypatch.setattr(extractor_quality, "run_eval", fake_run_eval)
    result = extractor_quality.evaluate_all([], k=5, embed_model=None)

    assert called == ["none", "chunk"]
    assert {item["mode"] for item in result["skipped"]} == {
        "llm",
        "llm_structured",
    }
    assert all(
        "explicit --include-llm" in item["reason"]
        for item in result["skipped"]
    )


def test_extractor_quality_explicit_llm_opt_in_attempts_provider_modes(monkeypatch):
    from eval import extractor_quality

    called = []

    def fake_run_eval(_cases, *, mode, **_kwargs):
        called.append(mode)
        return {"mode": mode}

    monkeypatch.setattr(extractor_quality, "run_eval", fake_run_eval)
    result = extractor_quality.evaluate_all(
        [],
        k=5,
        embed_model=None,
        include_llm=True,
    )

    assert called == ["none", "chunk", "llm", "llm_structured"]
    assert result["skipped"] == []
