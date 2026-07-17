"""Team-paywall regression tests for the intent-native agent WRITE surface
(commit 5a31389 added ``_paid("team")`` to /api/intent/remember and /api/intent/link).

These routes are the intent-native equivalent of /api/remember — an agent writing onto
this cloud instance's store — so a free / lapsed instance must not host them (402). The
shipping commit gated both routes but added no test either way; this pins the gate so a
future refactor can't silently reopen the free-write hole, and can't over-block a licensed
instance.
"""
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis import licensing
from engraphis.routes import v2_api


class _StubService:
    """Minimal stand-in so the ALLOW path exercises the gate, not the store."""

    def intent_remember(self, *args, **kwargs):
        return {"id": "mem_stub", "decision": "add"}

    def intent_link(self, *args, **kwargs):
        return {"linked": True}


@pytest.fixture(autouse=True)
def _reset_service():
    yield
    v2_api._service = None                              # don't leak the stub to other tests


def _client(monkeypatch, *, licensed):
    if licensed:
        monkeypatch.setattr("engraphis.licensing.require_feature", lambda *a, **k: None)
    else:
        def _deny(feature):
            raise licensing.LicenseError("Team license required", feature=feature)
        monkeypatch.setattr("engraphis.licensing.require_feature", _deny)
    v2_api.set_service(_StubService())
    app = FastAPI()
    app.include_router(v2_api.router)
    return TestClient(app)


def test_intent_remember_blocked_without_team_license(monkeypatch):
    client = _client(monkeypatch, licensed=False)
    r = client.post("/api/intent/remember", json={"text": "hello world"})
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["feature"] == "team"


def test_intent_link_blocked_without_team_license(monkeypatch):
    client = _client(monkeypatch, licensed=False)
    r = client.post("/api/intent/link",
                    json={"source_id": "mem_a", "target_id": "mem_b", "workspace": "default"})
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["feature"] == "team"


def test_intent_remember_allowed_with_team_license(monkeypatch):
    client = _client(monkeypatch, licensed=True)
    r = client.post("/api/intent/remember", json={"text": "hello world"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "mem_stub"


def test_intent_link_allowed_with_team_license(monkeypatch):
    client = _client(monkeypatch, licensed=True)
    r = client.post("/api/intent/link",
                    json={"source_id": "mem_a", "target_id": "mem_b", "workspace": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["linked"] is True
