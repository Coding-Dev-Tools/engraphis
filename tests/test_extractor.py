import pytest

from engraphis.backends.extractor import (
    LLMExtractor,
    PassthroughExtractor,
    StructuredLLMExtractor,
    get_extractor,
)
from engraphis.core.interfaces import Extractor, MemoryType


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
    types = {eng.store.get_memory(f["id"]).mtype for f in out["facts"]}
    assert types == {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}


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
    assert rec.metadata["entities"] == ["Engraphis", "SQLite"]
    assert rec.metadata["relations"][0]["target"] == "SQLite"
