"""HTTP-only coverage for host-owned adaptive context routing."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="adaptive context HTTP route needs the server extra")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.routes import v2_api


class _AdaptiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def adaptive_context(self, query: str, history: str, **kwargs):
        self.calls.append((query, history, kwargs))
        return {"mode": "history_bypass", "context": history[-32:], "sources": []}


def test_adaptive_context_is_a_host_http_endpoint_not_an_mcp_tool():
    service = _AdaptiveService()
    v2_api.set_service(service)
    app = FastAPI()
    app.include_router(v2_api.router)
    try:
        response = TestClient(app).post("/api/adaptive-context", json={
            "query": "What did we decide?",
            "history": "The host owns this conversation history.",
            "workspace": "acme",
            "repo": "api",
            "max_context_tokens": 512,
            "retrieval_token_budget": 256,
        })
    finally:
        v2_api._service = None

    assert response.status_code == 200
    assert response.json()["mode"] == "history_bypass"
    assert service.calls == [(
        "What did we decide?", "The host owns this conversation history.",
        {
            "workspace": "acme", "repo": "api", "session_id": None, "mtypes": None,
            "as_of": None, "valid_at": None, "known_at": None, "k": 8,
            "max_context_tokens": 512, "retrieval_token_budget": 256,
            "confidence_floor": 0.25, "retrieval_profile": "balanced",
            "candidate_depth": "adaptive", "diagnostics": False, "planning": "off",
            "mtype_limits": None,
        },
    )]
