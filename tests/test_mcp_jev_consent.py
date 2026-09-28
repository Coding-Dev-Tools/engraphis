"""MCP decisions preserve consent, uncertainty, fallback, and private error boundaries."""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
from engraphis import mcp_server as server
from engraphis.backends import jev_transport as transport


@pytest.mark.parametrize("kwargs", ({}, {"allow_remote": False},
                                    {"offline_mode": True, "allow_remote": True}))
def test_unapproved_or_offline_mcp_never_discovers_credentials(monkeypatch, kwargs):
    def forbidden(*args, **kw):
        pytest.fail("backend configuration must not be inspected without call permission")
    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    result = json.loads(server.engraphis_decide(kind="custom", state="Synthetic", **kwargs))
    assert result["is_fallback"] is True
    assert result["confidence"] is None
    assert result["selected"] is None
    assert result["advisory_only"] is True


def _client(monkeypatch, batch=None, error=None):
    calls = []
    def evaluate(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        if error is not None:
            raise error
        return batch
    monkeypatch.setattr(transport, "select_decision_client", lambda: (
        SimpleNamespace(evaluate=evaluate), "engraphis_cloud",
    ))
    return calls


@pytest.mark.parametrize("kind,key,result_key", (
    ("verify_support", "has_support", "supported"),
    ("verify_completion", "is_complete", "is_complete"),
))
def test_uncertain_remote_result_is_neither_success_nor_failure(monkeypatch, kind, key, result_key):
    batch = transport.CloudDecisionBatch(False, {}, {
        key: transport.SimpleSupportDecision(probability=0.5, confidence=0.0),
    })
    calls = _client(monkeypatch, batch)
    result = json.loads(server.engraphis_decide(
        kind=kind, state="Synthetic evidence", allow_remote=True, data_classification="public",
    ))
    assert calls[0][2] == {"model": transport.MODEL, "allow_remote": True,
                            "purpose": kind, "data_classification": "public"}
    assert result["is_fallback"] is False
    assert result["decision_status"] == "uncertain"
    assert result["confidence"] == 0.0
    assert result["confidence_source"] == "derived_decisiveness"
    assert result[result_key] is None


@pytest.mark.parametrize("batch,error,reason", (
    (transport.CloudDecisionBatch(True, {}, {}), None, "provider_fallback"),
    (transport.CloudDecisionBatch(False, {}, {}), None, "malformed_response"),
    (None, RuntimeError("private request and synthetic credential"), "remote_unavailable"),
    (None, transport.DecisionClientError("allowance_exhausted"), "allowance_exhausted"),
))
def test_remote_failures_cannot_look_like_verified_success(monkeypatch, batch, error, reason):
    _client(monkeypatch, batch, error)
    raw = server.engraphis_decide(kind="verify_support", state="Synthetic evidence",
                                 query="Synthetic", allow_remote=True)
    result = json.loads(raw)
    assert result["is_fallback"] is True
    assert result["decision_status"] == "local_fallback"
    assert result["fallback_reason"] == reason
    assert result["confidence"] is None
    assert "private request" not in raw and "synthetic credential" not in raw


def test_remote_advice_cannot_override_local_destructive_veto(monkeypatch):
    batch = transport.CloudDecisionBatch(False, {
        "category": transport.SimpleChoiceDecision("read_only", 0.99),
    }, {"is_safe": transport.SimpleSupportDecision(0.99, 0.98)})
    _client(monkeypatch, batch)
    result = json.loads(server.engraphis_decide(
        kind="guard_command", state="rm -rf / --no-preserve-root", allow_remote=True,
    ))
    assert result["allow_auto"] is False and result["escalate_to_user"] is True
    assert result["advisory_only"] is True
