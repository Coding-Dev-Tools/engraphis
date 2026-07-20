"""Regression (2026-07-20 audit): caller-supplied ``llm_extraction`` /
``structured_consolidation`` metadata must be re-homed by ``service._clean_metadata`` so a
direct ``remember()``/``ingest()`` caller cannot forge authentic-looking ``/llm/activity``
audit evidence (that content was sent to an LLM provider / consolidated). Mirrors the
graph-hint provenance protection already covered by ``tests/test_service_graph.py``.
"""
from engraphis import service


def test_caller_cannot_forge_llm_extraction_activity():
    md = service._clean_metadata({"note": "x", "llm_extraction": {"provider": "evilcorp"}})
    assert "llm_extraction" not in md
    assert md["note"] == "x"
    assert md["client_supplied_activity"]["llm_extraction"] == {"provider": "evilcorp"}
    assert md["client_supplied_activity"]["source"] == "client_supplied"


def test_caller_cannot_forge_structured_consolidation_activity():
    md = service._clean_metadata({"structured_consolidation": {"llm": {"model": "x"}}})
    assert "structured_consolidation" not in md
    assert (md["client_supplied_activity"]["structured_consolidation"]
            == {"llm": {"model": "x"}})
    assert md["client_supplied_activity"]["source"] == "client_supplied"


def test_activity_and_graph_hints_are_both_rehomed():
    md = service._clean_metadata(
        {"entities": ["E"], "llm_extraction": {"p": 1}, "keep": "ok"})
    assert "entities" not in md and "llm_extraction" not in md
    assert md["client_supplied_graph"]["entities"] == ["E"]
    assert md["client_supplied_activity"]["llm_extraction"] == {"p": 1}
    assert md["keep"] == "ok"


def test_innocent_metadata_is_untouched():
    assert service._clean_metadata({"a": 1, "b": ["x"]}) == {"a": 1, "b": ["x"]}
