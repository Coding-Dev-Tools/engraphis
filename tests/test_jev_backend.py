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
        self.contexts = []
        self.batch = SimpleNamespace(
            is_fallback=fallback,
            get_choice=lambda _: SimpleNamespace(selected=verdict, confidence=confidence),
            get_noul=lambda _: SimpleNamespace(probability=probability, confidence=confidence),
        )

    def evaluate(self, state, questions, *, model, allow_remote=False, purpose="custom", data_classification="internal"):
        self.calls.append((state, [question.to_dict() for question in questions], model))
        self.contexts.append((allow_remote, purpose, data_classification))
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


@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-5-5", True), ("claude-opus-5-5", True), ("claude-haiku-4-5", True),
    ("claude-sonnet-latest", False), ("claude-opus-5-5-latest", False), (" claude-opus-5-5", False),
    ("", False),
])
def test_claude_models_must_be_pinned_to_an_exact_id(model, expected):
    result = get_decision_backend("jev", client=FakeClient(), model=model)
    assert (result is not None) is expected


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


def test_legacy_support_tuple_preserves_probability_separately_from_confidence():
    client = FakeClient(confidence=0.6, probability=0.8)
    adapter = backend(client)

    result = adapter.verify_grounded_support_result(
        "database?", "Postgres", allow_remote=True,
    )
    assert result.status == "decision"
    assert result.value is True
    assert result.confidence == 0.6
    assert result.support_probability == 0.8
    assert adapter.verify_grounded_support(
        "database?", "Postgres", allow_remote=True,
    ) == (True, 0.8)


class LegacyClient(FakeClient):
    def evaluate(self, state, questions, *, model):
        self.calls.append((state, [question.to_dict() for question in questions], model))
        return self.batch


def test_original_injected_client_contract_still_produces_advisory_results():
    client = LegacyClient()
    adapter = backend(client)
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
    assert adapter.classify_contradiction("Use Postgres", memory(), allow_remote=True) == ("reinforces", 0.9)
    assert [call[2] for call in client.calls] == ["test-model-1.0", "test-model-1.0"]


@pytest.mark.parametrize("field", ["question_id", "prompt", "option"])
def test_sensitive_question_fields_are_screened_before_legacy_client_calls(field):
    client = LegacyClient()
    adapter = backend(client)
    question_id = "jev_review"
    prompt = "Choose the best bounded option."
    options = ("local", "remote")
    secret = "ghp_" + "A" * 20
    if field == "question_id":
        question_id = secret
    elif field == "prompt":
        prompt = f"Use this token: {secret}"
    else:
        options = ("local", secret)

    result = adapter.choose_option(
        "Synthetic public state", question_id=question_id, prompt=prompt,
        options=options, allow_remote=True,
    )

    assert result.status == "fallback"
    assert result.fallback_reason == "sensitive_content"
    assert client.calls == []


@pytest.mark.parametrize("variadic", [False, True])
def test_context_aware_clients_receive_the_authorized_request_context(variadic):
    class VariadicClient(FakeClient):
        def evaluate(self, state, questions, **options):
            return super().evaluate(state, questions, **options)

    client = VariadicClient() if variadic else FakeClient()
    adapter = backend(client)
    assert adapter.verify_grounded_support(
        "database?", "Postgres", allow_remote=True, data_classification="public",
    ) == (True, 0.9)
    assert adapter.classify_contradiction("Use Postgres", memory(), allow_remote=True) == ("reinforces", 0.9)
    assert client.contexts == [(True, "verify_support", "public"), (True, "classify_contradiction", "internal")]


@pytest.mark.parametrize("approved,offline,classification", [
    (False, False, "internal"), (1, False, "internal"), (True, True, "internal"),
    (True, False, "confidential"), (True, False, None), (True, False, []),
])
def test_denied_advisory_requests_do_not_inspect_client_credentials(approved, offline, classification):
    class PrivateClient(LegacyClient):
        @property
        def is_configured(self):
            pytest.fail("denied requests must not inspect client credentials")

    client = PrivateClient()
    adapter = backend(client, offline_mode=offline)
    assert adapter.verify_grounded_support(
        "database?", "Postgres", allow_remote=approved, data_classification=classification,
    ) == (False, 0.0)
    assert client.calls == []


@pytest.mark.parametrize("legacy", [False, True])
def test_provider_typeerror_never_retries_an_invocation(legacy, caplog):
    class ModernFailure(FakeClient):
        def evaluate(self, state, questions, **options):
            super().evaluate(state, questions, **options)
            raise TypeError("synthetic-private-provider-error")

    class LegacyFailure(LegacyClient):
        def evaluate(self, state, questions, *, model):
            super().evaluate(state, questions, model=model)
            raise TypeError("synthetic-private-provider-error")

    client = LegacyFailure() if legacy else ModernFailure()
    assert backend(client).verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    assert len(client.calls) == 1
    assert "synthetic-private-provider-error" not in caplog.text


def test_unsupported_client_signature_defers_without_invocation():
    class UnsupportedClient(FakeClient):
        def evaluate(self, state, questions, *, model, required_context):
            pytest.fail("unsupported signatures must not invoke the client")

    assert backend(UnsupportedClient()).verify_grounded_support(
        "database?", "Postgres", allow_remote=True,
    ) == (False, 0.0)


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


def test_cloud_decision_client_uses_saved_session_configuration(monkeypatch):
    from engraphis import cloud_session
    from engraphis.backends.jev_decision import create_cloud_decision_client
    configured = []
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: configured.append(kw) or True)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url",
                        lambda: "https://control.example.invalid")
    client = create_cloud_decision_client()
    assert configured == []
    assert client.is_configured is True
    assert configured == [{"require_compute": False}]
    assert client.allow_fallback is False


def test_typesafe_key_presence_is_not_an_implicit_backend_selection(monkeypatch):
    from engraphis.backends.jev_transport import select_decision_client
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-key")
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", "none")
    assert select_decision_client() == (None, "local_heuristic")
    client, name = select_decision_client("byok")
    assert client.is_configured and name == "typesafe_byok"
