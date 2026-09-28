"""Offline transport contracts: no keys, provider calls or credential discovery."""
import io
import json
from types import SimpleNamespace

import pytest

from engraphis import cloud_session, hosted_client
from engraphis.backends import jev_transport as transport
from engraphis.backends.jev_decision import DecisionQuestion


def _question(kind="noul"):
    return DecisionQuestion("q", "Assess the supplied evidence.", kind,
                            () if kind == "noul" else ("no", "yes"))


def _normalized(probability=0.9):
    return {"model": transport.MODEL, "is_fallback": False, "decisions": {"q": {
        "type": "noul", "probability": probability, "confidence": abs(2*probability-1),
        "confidence_source": "derived_decisiveness",
    }}}


@pytest.fixture
def managed(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: True)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url",
                        lambda: "https://control.example.invalid")

    def access(workspace, **kwargs):
        calls.append(("refresh", workspace, kwargs))
        return "synthetic-access-token", "org_synthetic", ""

    monkeypatch.setattr(cloud_session, "access_for_workspace", access)
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda value: value)

    def opener(*handlers):
        calls.append(("handlers", handlers))

        def open_request(request, timeout):
            calls.append(("request", request, timeout))
            response = io.BytesIO(json.dumps(_normalized()).encode())
            response.status = 200
            response.headers = {"Content-Type": "application/json"}
            return response
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(hosted_client, "build_pinned_https_opener", opener)
    return calls


def test_constructor_and_configuration_are_network_free_and_managed_refresh_is_bound(managed):
    client = transport.create_cloud_decision_client()
    assert client.is_configured and managed == []
    batch = client.evaluate("A synthetic statement", [_question()], model=transport.MODEL,
                            allow_remote=True, purpose="verify_support", data_classification="public")
    assert managed[0] == ("refresh", None, {"require_compute": False})
    _, request, timeout = managed[-1]
    assert request.full_url == "https://control.example.invalid/v1/jev/decide"
    assert request.get_header("Authorization") == "Bearer synthetic-access-token"
    assert 0 < timeout <= 15
    assert json.loads(request.data) == {
        "model": transport.MODEL, "state": "A synthetic statement", "questions": [_question().to_dict()],
        "allow_remote": True, "purpose": "verify_support", "data_classification": "public",
    }
    assert batch.get_noul("q").probability == 0.9
    assert batch.get_noul("q").confidence_source == "derived_decisiveness"


@pytest.mark.parametrize("kwargs", (
    {}, {"allow_remote": False}, {"allow_remote": 1},
    {"allow_remote": True, "data_classification": "secret"},
    {"allow_remote": True, "purpose": "silently_upload_memory"},
))
def test_consent_and_classification_are_checked_before_refresh(managed, kwargs):
    with pytest.raises(transport.DecisionClientError):
        transport.create_cloud_decision_client().evaluate(
            "Synthetic text", [_question()], model=transport.MODEL, **kwargs,
        )
    assert managed == []


@pytest.mark.parametrize("state,questions,model", (
    ("x"*16001, [_question()], transport.MODEL),
    ("api_key=synthetic0123456789", [_question()], transport.MODEL),
    ("Synthetic", [_question(), _question()], transport.MODEL),
    ("Synthetic", [DecisionQuestion("q", "secret=synthetic0123456789", "noul")], transport.MODEL),
    ("Synthetic", [DecisionQuestion("q", "Prompt", "score")], transport.MODEL),
    ("Synthetic", [DecisionQuestion("ghp_" + "A" * 36, "Prompt", "noul")], transport.MODEL),
    ("Synthetic", [_question()], "jev-latest"),
))
def test_invalid_or_sensitive_input_never_reaches_refresh(managed, state, questions, model):
    with pytest.raises(transport.DecisionClientError):
        transport.create_cloud_decision_client().evaluate(
            state, questions, model=model, allow_remote=True,
        )
    assert managed == []


def test_credential_origin_change_fails_without_using_token(managed, monkeypatch):
    values = iter(("https://first.example.invalid", "https://second.example.invalid"))
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", lambda: next(values))
    with pytest.raises(transport.DecisionClientError, match="session_changed"):
        transport.create_cloud_decision_client().evaluate(
            "Synthetic", [_question()], model=transport.MODEL, allow_remote=True,
        )
    assert len(managed) == 1 and managed[0][0] == "refresh"


def test_backend_modes_never_implicitly_choose_byok(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-personal-key")
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: True)
    for mode in ("none", "local"):
        assert transport.select_decision_client(mode) == (None, "local_heuristic")
    for mode in ("managed", "auto"):
        client, name = transport.select_decision_client(mode)
        assert isinstance(client, transport.EngraphisCloudDecisionClient)
        assert name == "engraphis_cloud"
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: False)
    assert transport.select_decision_client("auto") == (None, "local_heuristic")
    assert transport.select_decision_client("byok")[1] == "typesafe_byok"
    assert transport.select_decision_client("byok", offline_mode=True) == (None, "local_heuristic")


@pytest.mark.parametrize("change", (
    lambda body: body.update(model="jev-latest"),
    lambda body: body["decisions"]["q"].pop("confidence"),
    lambda body: body["decisions"]["q"].update(confidence=True),
    lambda body: body["decisions"]["q"].update(probability="0.9"),
    lambda body: body["decisions"]["q"].update(probability=float("nan")),
    lambda body: body["decisions"]["q"].update(confidence_source="provider"),
    lambda body: body["decisions"].update(extra={"type": "noul"}),
    lambda body: body.update(is_fallback="false"),
    lambda body: body.pop("is_fallback"),
))
def test_normalized_parser_rejects_malformed_values_without_default_confidence(change):
    body = _normalized()
    change(body)
    with pytest.raises(transport.DecisionClientError, match="malformed_response"):
        transport.parse_decision_batch(body, [_question()], normalized=True)


def test_uncertain_and_fallback_are_preserved():
    batch = transport.parse_decision_batch(_normalized(0.5), [_question()], normalized=True)
    assert batch.is_fallback is False and batch.get_noul("q").confidence == 0.0
    batch = transport.parse_decision_batch({"model": transport.MODEL, "is_fallback": True},
                                          [_question()], normalized=True)
    assert batch.is_fallback is True and batch.get_noul("q") is None


def test_provider_choice_score_and_noul_contracts():
    questions = [DecisionQuestion("choice", "Choose", "choice", ("no", "yes")),
                 DecisionQuestion("score", "Rate", "score", ("no", "yes")),
                 DecisionQuestion("noul", "Assess", "noul")]
    body = {"model": transport.MODEL, "answers": {
        "choice": {"type": "choice", "choice": "yes", "confidence": 0.8,
                   "probabilities": {"no": 0.2, "yes": 0.8}},
        "score": {"type": "score", "score": 0.7, "legend": {"0": "no", "1": "yes"},
                  "probabilities": {"0": 0.3, "1": 0.7}, "confidence": 0.8},
        "noul": {"type": "noul", "noul": 0.97},
    }}
    batch = transport.parse_decision_batch(body, questions, normalized=False)
    assert batch.get_choice("choice").selected == "yes"
    assert batch.get_score("score").score == 0.7
    assert batch.get_noul("noul").confidence == pytest.approx(0.94)
    body["answers"]["choice"].pop("confidence")
    with pytest.raises(transport.DecisionClientError):
        transport.parse_decision_batch(body, questions, normalized=False)


@pytest.mark.parametrize("raw", (b'{"model":"one","model":"two"}', b'{"score":NaN}',
                                b"x"*(transport.MAX_RESPONSE_BYTES+1)),
                         ids=("duplicate-key", "nonfinite", "oversized"))
def test_http_reader_bounds_and_strict_json(raw, monkeypatch):
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda value: value)
    response = io.BytesIO(raw)
    response.status = 200
    response.headers = {"Content-Type": "application/json"}
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener",
                        lambda *args: SimpleNamespace(open=lambda *a, **kw: response))
    with pytest.raises(transport.DecisionClientError, match="malformed_response"):
        transport._post_json("https://control.example.invalid/v1/jev/decide", "synthetic", {}, 1)


def test_redirects_and_transport_exceptions_never_echo_private_values(monkeypatch):
    with pytest.raises(transport.DecisionClientError, match="remote_unavailable"):
        transport._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
    def fail(value):
        raise RuntimeError("private request content and synthetic credential")
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", fail)
    with pytest.raises(transport.DecisionClientError) as error:
        transport._post_json("https://control.example.invalid", "synthetic", {}, 1)
    assert str(error.value) == "remote_unavailable"


def test_byok_native_payload_and_consent(monkeypatch):
    calls = []
    questions = [DecisionQuestion("q", "Rate the statement", "score", ("no", "yes"))]
    def post(url, token, payload, timeout):
        calls.append((url, token, payload, timeout))
        return {"model": transport.MODEL, "answers": {"q": {
            "type": "score", "score": 0.7, "legend": {"0": "no", "1": "yes"},
            "probabilities": {"0": 0.3, "1": 0.7}, "confidence": 0.8,
        }}}
    monkeypatch.setattr(transport, "_post_json", post)
    client = transport.TypeSafeDecisionClient(api_key="synthetic-personal-key")
    assert client.is_configured and not calls
    with pytest.raises(transport.DecisionClientError, match="remote_not_authorized"):
        client.evaluate("Synthetic", questions, model=transport.MODEL)
    assert not calls
    assert client.evaluate("Synthetic", questions, model=transport.MODEL,
                           allow_remote=True).get_score("q").score == 0.7
    assert calls[0][0] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0][2]["questions"] == {
        "q": {"type": "score", "instructions": "Rate the statement", "criteria": ["no", "yes"]},
    }


def test_choice_must_match_reported_probability_distribution():
    body = {"model": transport.MODEL, "answers": {"q": {
        "type": "choice", "choice": "no", "confidence": 0.8,
        "probabilities": {"no": 0.2, "yes": 0.8},
    }}}
    with pytest.raises(transport.DecisionClientError, match="malformed_response"):
        transport.parse_decision_batch(body, [_question("choice")], normalized=False)


def test_configuration_presence_honors_explicit_backend_and_managed_precedence(monkeypatch):
    from engraphis.config import Settings
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: True)
    for mode in ("none", "local"):
        assert not Settings(decision_backend=mode, typesafe_api_key="synthetic-key").has_decision_backend
    for mode in ("managed", "auto"):
        assert Settings(decision_backend=mode, typesafe_api_key="").has_decision_backend
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: False)
    assert not Settings(decision_backend="auto", typesafe_api_key="synthetic-key").has_decision_backend
    assert Settings(decision_backend="byok", typesafe_api_key="synthetic-key").has_decision_backend
    assert not Settings(decision_backend="byok", typesafe_api_key="offline").has_decision_backend


def test_https_loopback_managed_requests_disable_ambient_proxies(managed, monkeypatch):
    import urllib.request
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", lambda: "https://localhost:8443")
    transport.create_cloud_decision_client().evaluate(
        "Synthetic", [_question()], model=transport.MODEL, allow_remote=True,
    )
    handlers = next(value[1] for value in managed if value[0] == "handlers")
    assert any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {}
               for handler in handlers)


def test_url_validation_consumes_budget_before_network_open(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    def validate(url):
        clock[0] += 3.0
        return url
    def forbidden(*args, **kwargs):
        pytest.fail("expired validation budget reached network transport")
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", validate)
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=forbidden))
    with pytest.raises(transport.DecisionClientError, match="remote_timeout"):
        transport._post_json("https://synthetic.invalid/v1/jev/decide", "synthetic", {}, 2.0)


def test_slow_response_progress_does_not_renew_whole_request_timeout(monkeypatch):
    clock = [10.0]
    timeouts = []
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    class SlowResponse(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(
            settimeout=lambda value: timeouts.append(value),
        )))
        def read1(self, size=-1):
            clock[0] += 0.75
            return b" "
    response = SlowResponse()
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda value: value)
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(transport.DecisionClientError, match="remote_timeout"):
        transport._post_json("https://synthetic.invalid/v1/jev/decide", "synthetic", {}, 2.0)
    assert timeouts == [2.0, 1.25, 0.5]
    assert response.closed
