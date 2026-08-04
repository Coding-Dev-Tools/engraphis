"""Focused contract checks for the copied native Hermes provider."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "hermes" / "engraphis" / "__init__.py"


def _provider_module(monkeypatch):
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # noqa: D101 - Hermes's base is only a nominal contract here
        pass

    memory_provider.MemoryProvider = MemoryProvider
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    spec = importlib.util.spec_from_file_location("engraphis_hermes_provider_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Service:
    def __init__(self):
        self.calls = []

    def recall(self, query, **kwargs):
        self.calls.append(("recall", query, kwargs))
        return {"memories": [{"id": "mem_1", "content": "remember this choice"}]}

    def remember(self, content, **kwargs):
        self.calls.append(("remember", content, kwargs))
        return {"id": "mem_2", "stored": True}

    def secure_erase(self, memory_id, **kwargs):
        self.calls.append(("secure_erase", memory_id, kwargs))
        return {"id": memory_id, "status": "erased"}


def test_hermes_provider_imports_without_hermes_or_model_dependencies(monkeypatch):
    module = _provider_module(monkeypatch)
    provider = module.EngraphisMemoryProvider()

    assert provider.name == "engraphis"
    assert module._local_embed_model("sentence-transformers/all-MiniLM-L6-v2").startswith("local:")
    assert {tool["name"] for tool in provider.get_tool_schemas()} == {
        "engraphis_search", "engraphis_store", "engraphis_erase",
    }


def test_hermes_provider_uses_scoped_service_and_explicit_secure_erase(monkeypatch):
    module = _provider_module(monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_HERMES_WORKSPACE", "personal")
    monkeypatch.setenv("ENGRAPHIS_HERMES_REPO", "project")
    provider = module.EngraphisMemoryProvider()
    service = _Service()
    provider._service = service

    assert "[mem_1] remember this choice" in provider.prefetch("what did we choose")
    provider.sync_turn("Use the blue theme.", "I will keep that preference.", session_id="hermes-1")
    stored = json.loads(provider.handle_tool_call(
        "engraphis_store", {"text": "The theme is blue.", "keywords": ["theme"]},
    ))
    erased = json.loads(provider.handle_tool_call("engraphis_erase", {"memory_id": "mem_2"}))

    assert stored["id"] == "mem_2"
    assert erased == {"id": "mem_2", "status": "erased"}
    turn_call = next(call for call in service.calls if call[0] == "remember")
    assert turn_call[2]["workspace"] == "personal"
    assert turn_call[2]["repo"] == "project"
    assert turn_call[2]["scope"] == "repo"
    assert turn_call[2]["source"] == "agent"
    erase_call = next(call for call in service.calls if call[0] == "secure_erase")
    assert erase_call[2]["actor"] == "hermes"
