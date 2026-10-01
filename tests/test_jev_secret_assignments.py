"""Synthetic credential assignments never reach refresh or either Jev transport."""
import json
import time

import pytest

from engraphis import cloud_session
from engraphis.backends import jev_transport as transport
from engraphis.backends.jev_decision import DecisionQuestion


@pytest.fixture(params=["managed", "byok"])
def blocked_remote_client(request, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("sensitive text crossed the local validation boundary")

    monkeypatch.setattr(cloud_session, "credential_bound_control_url", forbidden)
    monkeypatch.setattr(cloud_session, "access_for_workspace", forbidden)
    monkeypatch.setattr(transport, "_post_json", forbidden)
    client = (transport.EngraphisCloudDecisionClient() if request.param == "managed" else
              transport.TypeSafeDecisionClient(api_key="synthetic-only",
                                               base_url="https://api.typesafe.ai"))
    return client, calls


def _assert_blocked(blocked_remote_client, caplog, state, questions):
    client, calls = blocked_remote_client
    with pytest.raises(transport.DecisionClientError) as caught:
        client.evaluate(state, questions, model=transport.MODEL, allow_remote=True)
    assert str(caught.value) == caught.value.code == "sensitive_content"
    assert calls == []
    assert caplog.text == ""


@pytest.mark.parametrize("name", [
    "API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_TOKEN", "DB_PASSWORD", "DB_PASSWD",
    "PASSWORD", "PASSWD", "SECRET", "SECRET_KEY", "SECRET_ACCESS_KEY",
    "AWS_SECRET_ACCESS_KEY", "CLIENT_SECRET", "PRIVATE_KEY", "SERVICE_PRIVATE_KEY",
    "TOKEN", "AUTH_TOKEN", "AUTHORIZATION_TOKEN", "SESSION_TOKEN", "AWS_SESSION_TOKEN",
    "REFRESH_TOKEN", "ID_TOKEN", "API_TOKEN", "AUTH", "AUTHORIZATION", "BEARER",
    "X-API-Key", "serviceApiKey", "databasePassword", "serviceSecretKey", "serviceToken",
])
def test_conventional_and_prefixed_credential_names_are_blocked(
    blocked_remote_client, caplog, name,
):
    _assert_blocked(blocked_remote_client, caplog, f"{name}=fixture",
                    [DecisionQuestion("q", "Evaluate the synthetic state", "noul")])


@pytest.mark.parametrize("value", [
    "OPENAI_API_KEY=abcdefgh12345678",
    "export OPENAI_API_KEY=abcdefgh12345678",
    "DB_PASSWORD=x",
    "PASSWORD=abcdefg",
    'DB_PASSWORD="P@ssw0rd!"',
    'PASSWORD="Ab!2$xy"',
    "DB_PASSWORD=P@ssw0rd!",
    'PASSWORD="two synthetic words"',
    'PASSWORD=" "',
    "$env:DB_PASSWORD = 'two synthetic words'",
    "${env:DB_PASSWORD} = 'P@ssw0rd!'",
    'set "DB_PASSWORD=abcdefg"',
    'os.environ["OPENAI_API_KEY"] = "synthetic"',
    "process.env['DB_PASSWORD'] = 'P@ssw0rd!'",
    json.dumps({"DB_PASSWORD": "P@ssw0rd!"}),
    json.dumps({"password": 'P@ss"w0rd!'}),
    json.dumps({"password": "two synthetic words"}),
    'DB_PASSWORD="synthetic\\\\path"',
    "Authorization: Basic c3ludGhldGljOm9ubHk=",
    "curl -H 'Authorization: Basic c3ludGhldGljOm9ubHk=' https://example.invalid",
    json.dumps({"Authorization": "opaque-synthetic-value"}),
    "PASSWORD=removed",
    "PASSWORD=withheld",
    "PASSWORD=<redacted>",
])
def test_assignment_forms_and_nonempty_literal_values_are_blocked(
    blocked_remote_client, caplog, value,
):
    _assert_blocked(blocked_remote_client, caplog, value,
                    [DecisionQuestion("q", "Evaluate the synthetic state", "noul")])


@pytest.mark.parametrize("field", ["state", "prompt", "choice", "score", "id"])
def test_every_outbound_raw_text_leaf_is_scanned(blocked_remote_client, caplog, field):
    # Scanning an outer json.dumps(value) would obscure the quoted key and miss
    # this raw embedded JSON. IDs use their valid restricted identifier syntax.
    private = json.dumps({"DB_PASSWORD": 'synthetic" phrase'})
    state = private if field == "state" else "Synthetic state"
    prompt = private if field == "prompt" else "Evaluate the synthetic state"
    kind = field if field in {"choice", "score"} else "noul"
    options = ("safe", private) if kind != "noul" else ()
    question_id = "ghp_" + "x" * 36 if field == "id" else "q"
    _assert_blocked(blocked_remote_client, caplog, state,
                    [DecisionQuestion(question_id, prompt, kind, options)])


@pytest.mark.parametrize("state", [
    "Keep API_KEY and DB_PASSWORD on the local device.",
    "The password is omitted and the token remains private.",
    "A secret key authenticates the request.",
    "DB_PASSWORD_LENGTH=12; API_KEY_COUNT=2; TOKEN_COUNT=100",
    "authorization_enabled=true; tokenization=enabled; author=synthetic",
    "DB_PASSWORD == expected_value",
    "DB_PASSWORD=", "DB_PASSWORD =   ", 'DB_PASSWORD=""', "DB_PASSWORD = ''",
    json.dumps({"DB_PASSWORD": ""}),
    "OPENAI_API_KEY=" + "\n",
])
def test_benign_prose_and_empty_assignments_remain_valid(state):
    payload = transport._request_payload(
        state, [DecisionQuestion("q", "Evaluate the synthetic state", "noul")],
        transport.MODEL, allow_remote=True, purpose="custom", data_classification="public",
    )
    assert payload["state"] == state


def test_long_benign_identifier_with_credential_words_is_bounded():
    state = ("long_identifier_" * 800) + "PASSWORD_LENGTH=12"
    assert len(state) < 16000
    payload = transport._request_payload(
        state, [DecisionQuestion("q", "Evaluate the synthetic state", "noul")],
        transport.MODEL, allow_remote=True, purpose="custom", data_classification="public",
    )
    assert payload["state"] == state


def test_whitespace_after_credential_name_does_not_backtrack_quadratically():
    state = "PASSWORD" + " " * 15880 + "is omitted"
    started = time.process_time()
    payload = transport._request_payload(
        state, [DecisionQuestion("q", "Evaluate the synthetic state", "noul")],
        transport.MODEL, allow_remote=True, purpose="custom", data_classification="public",
    )
    # CPU time avoids scheduler pauses; the ambiguous two-whitespace matcher
    # consumed about a CPU second for this valid-size, non-assignment input.
    assert time.process_time() - started < 0.25
    assert payload["state"] == state
