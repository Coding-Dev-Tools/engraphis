"""Tests for the deterministic, offline ChunkingExtractor (backends/extractor.py).

The chunker runs on the write/ingest path over untrusted content, so these tests pin
down not just correctness (headings, code fences, packing, overlap) but the safety
properties a security review depends on: determinism, the per-document chunk cap, and
control-character defanging.
"""
from engraphis.backends.extractor import (
    ChunkingExtractor,
    PassthroughExtractor,
    StructuredLLMExtractor,
    get_extractor,
)
from engraphis.core.interfaces import Extractor
from engraphis.core.textutil import estimate_tokens


def test_satisfies_extractor_protocol():
    assert isinstance(ChunkingExtractor(), Extractor)


def test_factory_selects_chunker_and_reads_env(monkeypatch):
    assert isinstance(get_extractor("chunk"), ChunkingExtractor)
    # unknown kinds still fall back to the offline passthrough
    assert isinstance(get_extractor("nope"), PassthroughExtractor)
    monkeypatch.setenv("ENGRAPHIS_CHUNK_TOKENS", "77")
    monkeypatch.setenv("ENGRAPHIS_CHUNK_OVERLAP", "9")
    monkeypatch.setenv("ENGRAPHIS_CHUNK_MAX", "5")
    ex = get_extractor("chunk")
    assert ex.target_tokens == 77 and ex.overlap_tokens == 9 and ex.max_chunks == 5


def test_empty_or_whitespace_returns_nothing():
    # engine.ingest treats [] as "extractor found nothing" and stores the raw text,
    # so an empty parse must not fabricate a chunk.
    assert ChunkingExtractor().extract("") == []
    assert ChunkingExtractor().extract("   \n\t  ") == []


def test_short_text_is_a_single_chunk_preserving_content():
    facts = ChunkingExtractor().extract("We use pnpm for frontend repos.")
    assert len(facts) == 1
    assert "pnpm" in facts[0].content
    assert facts[0].keywords  # derived, non-empty for substantive text


def test_determinism_same_input_same_output():
    text = ("# Alpha\n\nApples are red and crisp.\n\n## Beta\n\n"
            "Bananas are yellow.\n\n# Gamma\n\nCherries are small.")
    a = ChunkingExtractor(target_tokens=12, overlap_tokens=4).extract(text)
    b = ChunkingExtractor(target_tokens=12, overlap_tokens=4).extract(text)
    assert [(f.title, f.content, f.keywords) for f in a] == \
           [(f.title, f.content, f.keywords) for f in b]


def test_headings_become_titles_with_path():
    text = "# Alpha\n\nApples.\n\n## Beta\n\nBananas.\n\n# Gamma\n\nCherries."
    titles = [f.title for f in ChunkingExtractor().extract(text)]
    assert "Alpha" in titles and "Beta" in titles and "Gamma" in titles


def test_code_fence_is_kept_intact():
    text = (
        "# Title\n\nIntro paragraph.\n\n"
        "```python\n"
        "def f():\n"
        "    return 1\n"
        "\n"                       # blank line *inside* the fence must not split it
        "    # trailing comment\n"
        "```\n\n"
        "After paragraph."
    )
    facts = ChunkingExtractor(target_tokens=16).extract(text)
    code_facts = [f for f in facts if "def f():" in f.content]
    assert len(code_facts) == 1
    body = code_facts[0].content
    assert "return 1" in body and "# trailing comment" in body
    assert body.count("```") == 2  # both fences landed in the same chunk


def test_long_prose_splits_into_multiple_budgeted_chunks():
    # Ten ~equal sentences; a tight budget must produce several chunks, none absurdly
    # larger than the target (single sentences are never split mid-sentence).
    sentences = [f"Sentence number {i} describes topic {i} in some detail." for i in range(10)]
    text = " ".join(sentences)
    ex = ChunkingExtractor(target_tokens=20, overlap_tokens=0)
    facts = ex.extract(text)
    assert len(facts) > 1
    for f in facts:
        assert estimate_tokens(f.content) <= ex.target_tokens * 2


def test_overlap_carries_a_sentence_between_chunks():
    # Unique markers per sentence let us detect the carried-over overlap unambiguously.
    text = (". ".join(f"MARKER{i} alpha bravo charlie delta echo" for i in range(8)) + ".")
    with_overlap = ChunkingExtractor(target_tokens=16, overlap_tokens=12).extract(text)
    no_overlap = ChunkingExtractor(target_tokens=16, overlap_tokens=0).extract(text)
    assert len(with_overlap) > 1

    def markers_in(facts):
        seen = []
        for f in facts:
            seen.append({f"MARKER{i}" for i in range(8) if f"MARKER{i}" in f.content})
        return seen

    # With overlap, at least one marker appears in two adjacent chunks.
    ov = markers_in(with_overlap)
    assert any(ov[i] & ov[i + 1] for i in range(len(ov) - 1))
    # Without overlap, no marker is duplicated across chunks.
    no = markers_in(no_overlap)
    all_seen = [m for s in no for m in s]
    assert len(all_seen) == len(set(all_seen))


def test_chunk_cap_bounds_amplification():
    # A hostile document can't mint unbounded memories.
    text = "\n\n".join(f"Distinct paragraph {i} about widget {i}." for i in range(500))
    facts = ChunkingExtractor(target_tokens=16, overlap_tokens=0, max_chunks=7).extract(text)
    assert len(facts) <= 7


def test_control_characters_are_defanged():
    text = "Legit line.\n\nHidden\x00\x07escape\x1b payload here."
    facts = ChunkingExtractor(target_tokens=64).extract(text)
    joined = "".join(f.content for f in facts)
    assert "\x00" not in joined and "\x07" not in joined and "\x1b" not in joined
    assert "payload here" in joined


class _StructuredMockLLM:
    def __init__(self, payload):
        self.payload = payload

    def extract_json(self, prompt, schema):
        assert "Extract discrete" in prompt
        assert "facts" in schema.get("properties", {})
        return self.payload


class _FailingMockLLM:
    def extract_json(self, prompt, schema):
        raise RuntimeError("boom")


def test_structured_llm_extractor_validates_and_preserves_metadata():
    ex = StructuredLLMExtractor(_StructuredMockLLM({
        "facts": [{
            "content": "Engraphis uses PASETO for auth tokens.",
            "title": "Auth tokens",
            "mtype": "semantic",
            "importance": 0.8,
            "keywords": ["engraphis", "paseto", "auth"],
            "entities": ["Engraphis", "PASETO"],
            "relations": [{"source": "Engraphis", "relation": "uses", "target": "PASETO"}],
        }],
    }))
    facts = ex.extract("raw text")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.content == "Engraphis uses PASETO for auth tokens."
    assert fact.mtype.value == "semantic"
    assert fact.importance == 0.8
    assert fact.metadata["entities"] == ["Engraphis", "PASETO"]
    assert fact.metadata["relations"] == [
        {"source": "Engraphis", "relation": "uses", "target": "PASETO"},
    ]
    assert fact.metadata["structured_extraction"]["entities"] == ["Engraphis", "PASETO"]


def test_structured_llm_extractor_accepts_single_fact_object():
    ex = StructuredLLMExtractor(_StructuredMockLLM({
        "content": "Use pnpm for frontend packages.",
        "title": "Package manager",
        "mtype": "procedural",
        "importance": 2.0,
    }))
    fact = ex.extract("raw text")[0]
    assert fact.title == "Package manager"
    assert fact.mtype.value == "procedural"
    assert fact.importance == 1.0


def test_structured_llm_extractor_falls_back_to_chunking_on_failure():
    facts = StructuredLLMExtractor(_FailingMockLLM()).extract("# Title\n\nUse pnpm.")
    assert len(facts) == 1
    assert facts[0].title == "Title"
    assert "pnpm" in facts[0].content
