"""Intent-native writes stay part of the single-user local core."""
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.routes import v2_api


class _StubService:
    def intent_remember(self, *args, **kwargs):
        return {"id": "mem_stub", "decision": "add"}

    def intent_link(self, *args, **kwargs):
        return {"linked": True}


@pytest.fixture()
def client():
    v2_api.set_service(_StubService())
    app = FastAPI()
    app.include_router(v2_api.router)
    yield TestClient(app)
    v2_api._service = None


def test_intent_remember_has_no_client_side_team_paywall(client):
    response = client.post("/api/intent/remember", json={"text": "hello world"})
    assert response.status_code == 200
    assert response.json()["id"] == "mem_stub"


def test_intent_link_has_no_client_side_team_paywall(client):
    response = client.post(
        "/api/intent/link",
        json={"source_id": "mem_a", "target_id": "mem_b", "workspace": "default"},
    )
    assert response.status_code == 200
    assert response.json()["linked"] is True
