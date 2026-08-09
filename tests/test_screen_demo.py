"""Contracts for the truthful memory-continuity screen demo."""
from pathlib import Path

from demo.prepare_screen_demo import build_payload


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "demo" / "engraphis_screen_demo.html"
RECORDER = ROOT / "demo" / "record_screen_demo.mjs"


def test_screen_demo_payload_is_generated_from_a_complete_memory_flow():
    payload = build_payload()

    assert payload["session"]["bootstrap"]["summary"]
    assert payload["recall"]["memory"]["content"]
    assert len(payload["timeline"]) == 2
    assert payload["timeline"][0]["valid_to"] is not None
    assert payload["timeline"][1]["valid_to"] is None
    assert payload["why"]["supersedes"]
    assert payload["inspection"]["events"]


def test_screen_demo_recorder_refuses_unverified_fallback_evidence():
    html = HTML.read_text(encoding="utf-8")
    recorder = RECORDER.read_text(encoding="utf-8")

    assert 'id="payload-source"' in html
    assert "sample fallback data" in html
    assert 'hydrate(FALLBACK, "fallback")' in html
    assert "window.demoPayloadReady === true" in recorder
    assert 'payloadSource !== "generated"' in recorder
    assert "Refusing to record demo" in recorder
