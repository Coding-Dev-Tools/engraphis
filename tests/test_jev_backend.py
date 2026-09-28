"""The experimental adapter is offline-safe and never trusts fallback decisions."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from engraphis.backends.jev_decision import MAX_STATE_CHARS, JevDecisionBackend, get_decision_backend
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope


class FakeClient:
    is_configured = True
    allow_fallback = False

    def __init__(self, *, fallback=False, confidence=0.9, probability=0.9, verdict="reinforces"):
        self.calls = []
        self.batch = SimpleNamespace(
            is_fallback=fallback,
            get_choice=lambda _: SimpleNamespace(selected=verdict, confidence=confidence),
            get_noul=lambda _: SimpleNamespace(probability=probability, confidence=confidence),
        )

    def evaluate(self, state, questions, *, model):
        self.calls.append((state, [question.to_dict() for question in questions], model))
        return self.batch


def memory():
    return MemoryRecord(
        id="mem_1", scope=Scope.WORKSPACE, workspace_id="ws_1", mtype=MemoryType.SEMANTIC,
        title="Database", content="We use Postgres for primary user data storage.",
    )


def backend(client, **kwargs):
    return JevDecisionBackend(client=client, model="test-model-1.0", **kwargs)


def test_import_never_discovers_optional_sdk_or_sibling_checkout():
    script = """
import importlib.abc, sys
class DenySdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'jev_decision':
            raise AssertionError('optional SDK import attempted')
sys.meta_path.insert(0, DenySdk())
before = list(sys.path)
from engraphis.backends.jev_decision import JevDecisionBackend
assert sys.path == before
assert not JevDecisionBackend().is_available
assert JevDecisionBackend().verify_grounded_support('database?', '') == (False, 0.0)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_factory_requires_an_explicit_client_and_model(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", "jev")
    assert get_decision_backend() is None
    assert get_decision_backend(client=FakeClient()) is None
    assert get_decision_backend(client=FakeClient(), model="jev-latest") is None
    assert get_decision_backend(client=FakeClient(), model="jev-latest ") is None
    assert get_decision_backend(client=FakeClient(), model="JEV-LATEST") is None
    assert get_decision_backend("none", client=FakeClient(), model="test-model-1.0") is None
    result = get_decision_backend(client=FakeClient(), model="test-model-1.0")
    assert result is not None and result.identity == "engraphis.backend.jev.v1"


def test_offline_and_unapproved_requests_never_call_client():
    client = FakeClient()
    for adapter, approved in ((backend(client), False), (backend(client, offline_mode=True), True)):
        assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=approved) == (False, 0.0)
        assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=approved) == ("orthogonal", 0.0)
    assert client.calls == []


def test_explicitly_authorized_valid_response_is_advisory():
    client = FakeClient()
    adapter = backend(client)
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
    assert adapter.classify_contradiction("Use Postgres", memory(), allow_remote=True) == ("reinforces", 0.9)
    assert all(call[2] == "test-model-1.0" for call in client.calls)
    assert client.calls[1][1][0]["options"] == ["contradicts_and_supersedes", "reinforces", "orthogonal"]


@pytest.mark.parametrize("field,value", [
    ("confidence", float("nan")), ("confidence", float("inf")), ("confidence", True),
    ("confidence", -0.1), ("confidence", 1.1), ("confidence", 0.5),
    ("probability", float("nan")), ("probability", float("inf")), ("probability", True),
    ("probability", -0.1), ("probability", 1.1), ("probability", 0.5),
])
def test_invalid_or_uncertain_support_never_certifies_evidence(field, value):
    adapter = backend(FakeClient(**{field: value}))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    if field == "confidence":
        assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)


def test_fallbacks_and_unknown_verdicts_cannot_authorize_a_decision():
    for client in (FakeClient(fallback=True), FakeClient(verdict="delete_everything")):
        assert backend(client).classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)
    assert backend(FakeClient(fallback=True)).verify_grounded_support(
        "database?", "Bananas are yellow.", allow_remote=True,
    ) == (False, 0.0)
    client = FakeClient()
    client.allow_fallback = True
    assert not backend(client).is_available


def test_missing_malformed_and_failed_responses_defer_without_logging_payloads(caplog):
    for batch in (None, SimpleNamespace(is_fallback=False)):
        client = FakeClient()
        client.batch = batch
        assert backend(client).verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    class FailingClient(FakeClient):
        def evaluate(self, *args, **kwargs):
            raise RuntimeError("private request content")
    adapter = backend(FailingClient())
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)
    assert "private request content" not in caplog.text


@pytest.mark.parametrize("query,evidence", [("", "Postgres"), ("database?", " "), ("database?", "x" * MAX_STATE_CHARS)])
def test_empty_and_oversized_inputs_do_not_leave_the_process(query, evidence):
    client = FakeClient()
    assert backend(client).verify_grounded_support(query, evidence, allow_remote=True) == (False, 0.0)
    assert client.calls == []


def test_cloud_decision_client_configuration(monkeypatch):
    import io
    from engraphis.backends.jev_decision import create_cloud_decision_client, DecisionQuestion

    # Unconfigured
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    client = create_cloud_decision_client(token="")
    assert client.is_configured is False
    assert client.allow_fallback is False

    # Configured
    client_configured = create_cloud_decision_client(token="test-token", control_url="https://api.engraphis.com")
    assert client_configured.is_configured is True
    assert client_configured.allow_fallback is False

    # Mock evaluate response
    mock_payload = b'{"decisions": {"q1": {"type": "choice", "selected": "reinforces", "confidence": 0.95}}}'
    mock_resp = io.BytesIO(mock_payload)
    mock_resp.status = 200

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: mock_resp)

    q = DecisionQuestion("q1", "prompt", "choice", ("reinforces", "orthogonal"))
    batch = client_configured.evaluate("test state", [q], model="test-model-1.0")
    assert batch.is_fallback is False
    assert batch.get_choice("q1").selected == "reinforces"
    assert batch.get_choice("q1").confidence == 0.95


def test_typesafe_decision_client_configuration(monkeypatch):
    import io
    import urllib.request
    from engraphis.backends.jev_decision import create_typesafe_decision_client, DecisionQuestion, JevDecisionBackend

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    # Unconfigured
    client = create_typesafe_decision_client(api_key="")
    assert client.is_configured is False
    assert client.allow_fallback is False

    # Configured via env
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-api-key-xyz")
    client_env = create_typesafe_decision_client()
    assert client_env.is_configured is True
    assert client_env.allow_fallback is False

    # Mock evaluate response with TypeSafe official 'answers' format
    mock_payload = (
        b'{"model":"jev-1.13.0","answers":{'
        b'"safe":{"type":"noul","noul":0.97},'
        b'"rel":{"type":"choice","choice":"reinforces","confidence":0.99}'
        b'}}'
    )
    mock_resp = io.BytesIO(mock_payload)
    mock_resp.status = 200
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: mock_resp)

    q1 = DecisionQuestion("safe", "Is safe?", "noul")
    q2 = DecisionQuestion("rel", "Relation?", "choice", ("reinforces", "orthogonal"))
    batch = client_env.evaluate("some state", [q1, q2], model="jev-1.13.0")
    assert batch.is_fallback is False
    assert batch.get_noul("safe").probability == 0.97
    assert batch.get_choice("rel").selected == "reinforces"
    assert batch.get_choice("rel").confidence == 0.99

    # Verify integration with JevDecisionBackend
    backend = JevDecisionBackend(client=client_env, model="jev-1.13.0")
    assert backend.is_available is True



