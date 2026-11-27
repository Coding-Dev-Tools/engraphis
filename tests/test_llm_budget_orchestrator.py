"""Tests for the managed LLM budget orchestrator, complexity gate, 429 circuit breaker,
content-hash cache, prompt trimming, and pooled client reuse."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

httpx = pytest.importorskip("httpx", reason="LLM client tests require httpx")

from engraphis.ai_context import build_proactive_context  # noqa: E402
from engraphis.backends.extractor import LLMExtractor  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.core.engine import MemoryEngine  # noqa: E402
from engraphis.llm import client as llm_client  # noqa: E402
from engraphis.llm.budget import (  # noqa: E402
    LLMResultCache,
    ManagedAllowanceTracker,
    _parse_epoch_timestamp,
    llm_result_cache,
    managed_allowance_tracker,
    should_llm_extract,
)
from engraphis.llm.client import (  # noqa: E402
    DEFAULT_MANAGED_MODEL,
    LLMClient,
    MANAGED_MAX_MESSAGE_CHARS,
    _LLMProviderError,
    _trim_messages_for_managed_limit,
    close_shared_managed_llm_client,
    get_shared_managed_llm_client,
)
from engraphis.service import MemoryService  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_llm_orchestrator_state():
    managed_allowance_tracker().reset()
    llm_result_cache().clear()
    llm_client._JSON_MODE_UNSUPPORTED.clear()
    close_shared_managed_llm_client()
    yield
    managed_allowance_tracker().reset()
    llm_result_cache().clear()
    llm_client._JSON_MODE_UNSUPPORTED.clear()
    close_shared_managed_llm_client()


def _wire_managed_session(monkeypatch):
    from engraphis import cloud_session, hosted_client

    monkeypatch.setattr(cloud_session, "configured", lambda require_compute=False: True)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", lambda: "https://api.engraphis.com")
    monkeypatch.setattr(cloud_session, "saved_entitlement", lambda: {"plan": "pro", "status": "active", "cloud_access_active": True})
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda url: url)
    monkeypatch.setattr(cloud_session, "access_for_workspace", lambda ws, require_compute=False, deadline=None: ("rotating-token", "org_pro", None))


def _json_resp(status: int, payload: dict, url: str = "https://api.engraphis.com/v1/llm/chat/completions", headers=None):
    return httpx.Response(
        status, request=httpx.Request("POST", url),
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **(headers or {})},
    )


def test_429_rolling_allowance_exhausted_fails_fast_and_trips_breaker(monkeypatch):
    _wire_managed_session(monkeypatch)
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))
    future_release = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    http_calls: list[dict] = []

    def fake_post(url, *, json=None, headers=None, **kwargs):  # noqa: A002
        http_calls.append({"url": url, "body": json})
        return _json_resp(429, {"detail": {"code": "llm_rolling_allowance_exhausted", "next_release_at": future_release}}, url=url)

    with LLMClient(provider="managed", api_key="") as client:
        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(_LLMProviderError) as exc1:
            client.chat([{"role": "user", "content": "Summarize recent changes"}])
        assert exc1.value.status == 429 and "allowance" in str(exc1.value).lower()
        assert len(http_calls) == 1 and sleep_calls == []
        snap = managed_allowance_tracker().snapshot()
        assert snap["exhausted"] is True and snap["remaining_requests"] == 0
        t0 = time.monotonic()
        with pytest.raises(_LLMProviderError) as exc2:
            client.chat([{"role": "user", "content": "Another prompt"}])
        assert exc2.value.status == 429 and len(http_calls) == 1 and sleep_calls == []
        assert (time.monotonic() - t0) * 1000.0 < 100.0


def test_transient_non_quota_429_retries_and_reuses_request_key(monkeypatch):
    _wire_managed_session(monkeypatch)
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))
    seen_keys: list[str] = []

    def fake_post(url, *, json=None, headers=None, **kwargs):  # noqa: A002
        seen_keys.append(json["request_key"])
        if len(seen_keys) == 1:
            return _json_resp(429, {"detail": {"code": "upstream_busy"}}, url=url, headers={"retry-after": "1"})
        return _json_resp(200, {
            "choices": [{"message": {"content": "recovered"}}],
            "allowance": {"remaining_requests": 88, "windows": [{"seconds": 86400, "limit": 100, "remaining_requests": 88}]},
        }, url=url)

    with LLMClient(provider="managed", api_key="") as client:
        monkeypatch.setattr(client._http, "post", fake_post)
        assert client.chat([{"role": "user", "content": "Retry test"}]) == "recovered"
        assert len(seen_keys) == 2 and seen_keys[0] == seen_keys[1] and re.fullmatch(r"[0-9a-f]{32}", seen_keys[0])
        assert len(sleep_calls) == 1
        assert client.chat([{"role": "user", "content": "Retry test"}]) == "recovered"
        assert len(seen_keys) == 3 and seen_keys[2] != seen_keys[0]


def test_allowance_updates_headroom_and_managed_priority(monkeypatch):
    _wire_managed_session(monkeypatch)
    monkeypatch.delenv("ENGRAPHIS_LLM_INTERACTIVE_RESERVE", raising=False)
    release_iso = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    calls: list[dict] = []

    def fake_post(url, *, json=None, headers=None, **kwargs):  # noqa: A002
        calls.append(json)
        return _json_resp(200, {
            "choices": [{"message": {"content": "ok"}}],
            "allowance": {"remaining_requests": 42, "windows": [{"seconds": 86400, "limit": 100, "remaining_requests": 42, "next_release_at": release_iso}]},
        }, url=url)

    with LLMClient(provider="managed", api_key="") as client:
        monkeypatch.setattr(client._http, "post", fake_post)
        assert client.chat([{"role": "user", "content": "Check allowance"}]) == "ok"
        snap = managed_allowance_tracker().snapshot()
        assert snap["remaining_requests"] == 42 and snap["limit"] == 100 and snap["exhausted"] is False and snap["next_release_at"] is not None

        managed_allowance_tracker().record_allowance({"remaining_requests": 10, "limit": 100})
        with pytest.raises(_LLMProviderError) as exc_info:
            client.chat([{"role": "user", "content": "Background work"}], priority="background")
        assert exc_info.value.status == 429 and len(calls) == 1
        assert client.chat([{"role": "user", "content": "User Q&A"}], priority="interactive") == "ok"
        assert len(calls) == 2

    tracker = ManagedAllowanceTracker()
    tracker.record_allowance({"remaining_requests": 16, "limit": 100})
    assert tracker.can_call(priority="background") == (True, "ok")
    tracker.record_allowance({"remaining_requests": 15, "limit": 100})
    assert tracker.can_call(priority="background") == (False, "headroom_reserved_for_interactive")
    assert tracker.can_call(priority="interactive") == (True, "ok")
    tracker.record_allowance({"remaining_requests": 0, "limit": 100})
    assert tracker.can_call(priority="interactive") == (False, "rolling_allowance_exhausted")
    monkeypatch.setenv("ENGRAPHIS_LLM_INTERACTIVE_RESERVE", "100")
    tracker.record_allowance({"remaining_requests": 50, "limit": 100})
    assert tracker.can_call(priority="background") == (False, "headroom_reserved_for_interactive")
    tracker.record_allowance({"remaining_requests": 51, "limit": 100})
    assert tracker.can_call(priority="background") == (True, "ok")


@pytest.mark.parametrize("atomic_fact", [
    "We use pnpm for frontend repos.",
    "Use PostgreSQL 16.2 for the billing ledger.",
    "Deployment target is api.engraphis.com on fly.io.",
    "Prefer SQLite, e.g. for offline local dev.",
    "PASETO token lifetime is 15 minutes",
])
def test_complexity_gate_skips_short_atomic_facts(monkeypatch, atomic_fact):
    monkeypatch.delenv("ENGRAPHIS_LLM_EXTRACT_MIN_CHARS", raising=False)
    assert should_llm_extract(atomic_fact) == (False, "short_atomic_fact")


@pytest.mark.parametrize("rich_text", [
    "We use PostgreSQL 16 for billing. All schema migrations must pass staging verification first.",
    "- Use Postgres 16\n- Use Redis for queues",
    "1. Rotate staging keys\n2. Verify webhook signatures",
    "User: switch auth to PASETO\nAssistant: updated middleware and tests",
    "Decision: freeze deploys on Fridays until canary alerts stabilize",
    "TODO: add circuit breaker metrics to health endpoint",
    "Auth uses PASETO tokens with 15m TTL; refresh keys rotate every 24 hours in staging.",
    "A" * 185,
])
def test_complexity_gate_accepts_rich_inputs_and_env_zero(monkeypatch, rich_text):
    monkeypatch.delenv("ENGRAPHIS_LLM_EXTRACT_MIN_CHARS", raising=False)
    assert should_llm_extract(rich_text)[0] is True
    monkeypatch.setenv("ENGRAPHIS_LLM_EXTRACT_MIN_CHARS", "0")
    assert should_llm_extract("Short atomic fact.") == (True, "min_chars_disabled")


def test_result_cache_lru_ttl_dedup_and_malformed_bypass(monkeypatch):
    _wire_managed_session(monkeypatch)
    now = [1000.0]
    cache = LLMResultCache(max_entries=2, ttl_seconds=60.0, clock=lambda: now[0])
    raw_prompt = "Unique sensitive architectural prompt about billing-engine-xyz"
    k1 = cache.make_key("extract_json", "managed", "fast", raw_prompt)
    assert raw_prompt not in repr(k1) and len(k1[3]) == 64
    cache.put(k1, {"facts": [{"content": "A"}]})
    hit, val = cache.get(k1)
    assert hit is True and val == {"facts": [{"content": "A"}]}
    val["facts"].append({"content": "MUTATED"})
    assert cache.get(k1)[1] == {"facts": [{"content": "A"}]}
    k2, k3 = cache.make_key("extract_json", "managed", "fast", "second"), cache.make_key("extract_json", "managed", "fast", "third")
    cache.put(k2, {"facts": [{"content": "B"}]})
    cache.get(k1)
    cache.put(k3, {"facts": [{"content": "C"}]})
    assert cache.get(k2) == (False, None) and cache.get(k1)[0] is True and cache.get(k3)[0] is True
    now[0] += 61.0
    assert cache.get(k1) == (False, None)

    schema = {"type": "object", "properties": {"facts": {"type": "array"}}}
    calls = [0, 0]
    with LLMClient(provider="managed", api_key="", profile="fast") as client:
        monkeypatch.setattr(client._http, "post", lambda url, **kw: (calls.__setitem__(0, calls[0] + 1), _json_resp(200, {"choices": [{"message": {"content": '{"facts": [{"content": "Cached fact."}]}'}}]}, url=url))[1])
        assert client.extract_json("Extract doc.", schema) == {"facts": [{"content": "Cached fact."}]}
        assert client.extract_json("Extract doc.", schema) == {"facts": [{"content": "Cached fact."}]}
        assert calls[0] == 1
        monkeypatch.setattr(client._http, "post", lambda url, **kw: (calls.__setitem__(1, calls[1] + 1), _json_resp(200, {"choices": [{"message": {"content": "not valid json"}}]}, url=url))[1])
        assert "raw" in client.extract_json("Malformed prompt.", schema) and "raw" in client.extract_json("Malformed prompt.", schema)
        assert calls[1] == 2


def test_managed_prompt_trimmer_and_profiles(monkeypatch):
    _wire_managed_session(monkeypatch)
    trimmed = _trim_messages_for_managed_limit([
        {"role": "system", "content": "SYSTEM_INSTRUCTIONS: output JSON only. " + ("S" * 1200)},
        {"role": "user", "content": "OLD_HISTORY: " + ("H" * 15_000)},
        {"role": "user", "content": "TASK_START: " + ("U" * 32_000) + " :TASK_END"},
    ], max_chars=MANAGED_MAX_MESSAGE_CHARS)
    assert sum(len(m["content"]) for m in trimmed) <= MANAGED_MAX_MESSAGE_CHARS
    assert trimmed[0]["role"] == "system" and "SYSTEM_INSTRUCTIONS" in trimmed[0]["content"]
    assert any(m["role"] == "user" and "TASK_START" in m["content"] and "TASK_END" in m["content"] for m in trimmed)

    captured_bodies: list[dict] = []
    with LLMClient(provider="managed", api_key="") as client:
        monkeypatch.setattr(client._http, "post", lambda url, *, json=None, **kw: (captured_bodies.append(json), _json_resp(200, {"choices": [{"message": {"content": "trimmed-ok"}}]}, url=url))[1])
        assert client.chat([{"role": "user", "content": "HEAD_MARKER " + ("x" * 35_000) + " TAIL_MARKER"}], system="You are helpful.", profile="fast") == "trimmed-ok"
        client.chat([{"role": "user", "content": "Default model check"}])
    assert captured_bodies[0]["model"] == "fast" and "openrouter" not in json.dumps(captured_bodies[0]).lower()
    assert sum(len(m["content"]) for m in captured_bodies[0]["messages"]) <= MANAGED_MAX_MESSAGE_CHARS
    assert re.fullmatch(r"[0-9a-f]{32}", captured_bodies[0]["request_key"]) and captured_bodies[1]["model"] == DEFAULT_MANAGED_MODEL


def test_engine_ingest_gates_auto_managed_without_affecting_explicit_extractors(monkeypatch, tmp_path):
    _wire_managed_session(monkeypatch)
    for var in ("ENGRAPHIS_EXTRACTOR", "ENGRAPHIS_LLM_AUTO_EXTRACT", "ENGRAPHIS_LLM_EXTRACT_MIN_CHARS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    extract_calls: list[str] = []

    class _SpyManagedLLM:
        provider, model, api_key, has_credentials = "managed", DEFAULT_MANAGED_MODEL, "", True

        def __init__(self, *args, **kwargs):
            self._shared_managed = True

        def close(self):
            raise AssertionError("shared pooled client must not be closed per ingest")

        def extract_json(self, prompt, schema, *, timeout=None):
            extract_calls.append(prompt)
            return {"facts": [{"content": "Billing runs on PostgreSQL 16.", "title": "Billing DB", "mtype": "semantic", "importance": 0.8, "keywords": ["postgres"], "entities": ["PostgreSQL 16"], "relations": []}]}

    monkeypatch.setattr(llm_client, "LLMClient", _SpyManagedLLM)
    svc = MemoryService.create(str(tmp_path / "gate.db"), embed_model="", extractor="none")
    try:
        short_res = svc.ingest("Billing runs on PostgreSQL 16.", workspace="ws_gate")
        assert len(extract_calls) == 0 and short_res["extracted"] is False and short_res["count"] == 1
        rec = svc.store.get_memory(short_res["facts"][0]["id"])
        assert rec is not None and "extraction_fallback" not in rec.metadata
        rich_res = svc.ingest("Billing runs on PostgreSQL 16. All migrations require staging sign-off.", workspace="ws_gate")
        assert len(extract_calls) == 1 and rich_res["extracted"] is True
        managed_allowance_tracker().record_allowance({"remaining_requests": 10, "limit": 100})
        reserved_res = svc.ingest("Auth uses PASETO tokens. Refresh tokens rotate on every login.", workspace="ws_gate")
        assert len(extract_calls) == 1 and reserved_res["extracted"] is False
    finally:
        svc.close()

    explicit_calls: list[str] = []

    class _ExplicitFakeLLM:
        provider, model = "openai", "gpt-4o-mini"

        def chat(self, messages, system=None, **kw):
            explicit_calls.append(messages[0]["content"])
            return '{"facts": [{"content": "Explicitly extracted short fact."}]}'

    eng = MemoryEngine.create(":memory:")
    try:
        eng.extractor = LLMExtractor(_ExplicitFakeLLM())
        out = eng.ingest("Short fact.", workspace_id=eng.store.get_or_create_workspace("ws_explicit"))
        assert len(explicit_calls) == 1 and out["extracted"] is True
    finally:
        eng.close()


def test_proactive_context_synthesis_enforces_18000_char_budget():
    captured_prompts: list[str] = []

    class _CapturingLLM:
        def chat(self, messages, system=None, **kw):
            captured_prompts.append(messages[0]["content"])
            return "- Grounded synthesis bullet [1]."

    huge_memories = [{"id": f"mem_{i}", "title": f"Memory {i}", "content": "M" * 1_100} for i in range(24)]
    out = build_proactive_context(
        task="T" * 9_500, agent_state="A" * 19_500, memories=huge_memories,
        last_session={"summary": "S" * 5_000, "outcome": "O" * 3_000, "open_threads": ["R" * 400] * 6},
        llm=_CapturingLLM(), synthesize=True,
    )
    assert out["synthesized"] is True and len(captured_prompts) == 1 and len(captured_prompts[0]) <= 18_000 and "[1]" in captured_prompts[0]


def test_cache_rejects_unauthenticated_bypass_and_isolates_scope(monkeypatch):
    _wire_managed_session(monkeypatch)
    from engraphis import cloud_session

    monkeypatch.setattr(cloud_session, "saved_session_digest", lambda: "digest-a")
    schema = {"type": "object", "properties": {"v": {"type": "string"}}}
    calls: list[str] = []

    def make_post(tag):
        return lambda url, **kw: (calls.append(f"{tag}:{url}"), _json_resp(200, {"choices": [{"message": {"content": f'{{"v":"{tag}"}}'}}]}, url=url))[1]

    with LLMClient(provider="openai", model="gpt-4o-mini", api_key="sk-a", base_url="https://a.example/v1") as c_a:
        monkeypatch.setattr(c_a._http, "post", make_post("A"))
        assert c_a.extract_json("same prompt", schema) == {"v": "A"}
    with LLMClient(provider="openai", model="gpt-4o-mini", api_key="", base_url="https://a.example/v1") as c_unauth:
        with pytest.raises(ValueError, match="No LLM API key"):
            c_unauth.extract_json("same prompt", schema)
    with LLMClient(provider="openai", model="gpt-4o-mini", api_key="sk-a", base_url="https://b.example/v1") as c_b:
        monkeypatch.setattr(c_b._http, "post", make_post("B"))
        assert c_b.extract_json("same prompt", schema) == {"v": "B"}
    assert len(calls) == 2

    with LLMClient(provider="managed", api_key="", profile="fast") as c_m:
        monkeypatch.setattr(c_m._http, "post", make_post("M1"))
        assert c_m.chat([{"role": "user", "content": "hi"}], use_cache=True) == '{"v":"M1"}'
    monkeypatch.setattr(cloud_session, "configured", lambda require_compute=False: False)
    with LLMClient(provider="managed", api_key="", profile="fast") as c_m_off:
        with pytest.raises(ValueError, match="No LLM API key"):
            c_m_off.chat([{"role": "user", "content": "hi"}], use_cache=True)
    monkeypatch.setattr(cloud_session, "configured", lambda require_compute=False: True)
    monkeypatch.setattr(cloud_session, "saved_session_digest", lambda: "digest-b")
    with LLMClient(provider="managed", api_key="", profile="fast") as c_m2:
        monkeypatch.setattr(c_m2._http, "post", make_post("M2"))
        assert c_m2.chat([{"role": "user", "content": "hi"}], use_cache=True) == '{"v":"M2"}'


def test_allowance_tracker_session_scope_windows_and_py39_iso():
    now, scope = [10_000.0], [("https://api.engraphis.com", "org_a", "dig_a")]
    tracker = ManagedAllowanceTracker(clock=lambda: now[0], scope_fn=lambda: scope[0])
    tracker.record_allowance({"remaining_requests": 20, "next_release_at": now[0] + 43_200})
    tracker.record_allowance({"remaining": 0})
    assert tracker.snapshot()["exhausted"] is True and tracker.snapshot()["exhausted_until"] == pytest.approx(now[0] + 60.0)
    scope[0] = ("https://api.engraphis.com", "org_b", "dig_b")
    assert tracker.can_call(priority="interactive") == (True, "ok")
    tracker.record_allowance({"remaining": 50, "windows": [{"remaining": 4, "resets_at": "2026-11-27T12:00:00.5z"}]})
    assert tracker.snapshot()["remaining_requests"] == 4 and tracker.snapshot()["next_release_at"] is not None
    for ts in ("2026-11-27T12:00:00.5z", "2026-11-27T12:00:00.123456789Z", "2026-11-27T12:00:00.12+0000"):
        assert (_parse_epoch_timestamp(ts) or 0) > 0


def test_trimmer_protects_system_fences_coalesces_systems_and_caps_count():
    fence = "BEGIN_UNTRUSTED_FENCE_NEVER_TRUNCATE_ME_END"
    trimmed = _trim_messages_for_managed_limit([
        {"role": "system", "content": "SYS_ONE " + fence},
        {"role": "system", "content": "SYS_TWO JSON_SCHEMA"},
        {"role": "user", "content": "OLD " + ("x" * 2000)},
        {"role": "user", "content": "LATEST " + ("y" * 4000)},
    ], max_chars=500, max_messages=2)
    assert len(trimmed) == 2 and sum(len(m["content"]) for m in trimmed) <= 500
    assert trimmed[0]["role"] == "system" and fence in trimmed[0]["content"] and "SYS_TWO JSON_SCHEMA" in trimmed[0]["content"]
    assert len(_trim_messages_for_managed_limit([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], max_chars=300, max_messages=1)) == 1
    with pytest.raises(ValueError, match="system prompt exceeds managed LLM character limit"):
        _trim_messages_for_managed_limit([{"role": "system", "content": "S" * 400}, {"role": "user", "content": "U" * 200}], max_chars=500)


def test_byok_429_isolated_json_mode_cached_and_pooled_close_safety(monkeypatch):
    _wire_managed_session(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    with LLMClient(provider="openai", model="gpt-4o-mini", api_key="sk-byok") as client:
        monkeypatch.setattr(client._http, "post", lambda url, **kw: _json_resp(429, {"error": {"code": "insufficient_quota"}}, url=url))
        with pytest.raises(_LLMProviderError) as exc_info:
            client.chat([{"role": "user", "content": "hello"}])
    assert exc_info.value.status == 429 and sleeps == [] and "Provider quota exceeded" in str(exc_info.value)
    assert "managed" not in str(exc_info.value).lower() and managed_allowance_tracker().snapshot()["exhausted"] is False

    seen: list[dict] = []

    def fake_post(url, *, json=None, **kw):  # noqa: A002
        seen.append(dict(json))
        return _json_resp(400, {"error": {"message": "unsupported"}}, url=url) if "response_format" in json else _json_resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]}, url=url)

    with LLMClient(provider="openai", model="custom-no-json", api_key="sk-test") as client:
        monkeypatch.setattr(client._http, "post", fake_post)
        assert client.extract_json("prompt 1", {"type": "object"}) == {"ok": True}
        assert client.extract_json("prompt 2", {"type": "object"}) == {"ok": True}
    assert len(seen) == 3 and "response_format" in seen[0] and "response_format" not in seen[1] and "response_format" not in seen[2]

    secret_calls = [0]
    with LLMClient(provider="managed", api_key="", profile="fast") as mclient:
        monkeypatch.setattr(mclient._http, "post", lambda url, **kw: (secret_calls.__setitem__(0, secret_calls[0] + 1), _json_resp(400, {"detail": {"code": "secret_detected"}}, url=url))[1])
        with pytest.raises(_LLMProviderError) as sec_exc:
            mclient.extract_json("contains secret", {"type": "object"})
    assert sec_exc.value.status == 400 and secret_calls[0] == 1

    c1 = get_shared_managed_llm_client(profile="fast", default_priority="background")
    assert c1 is get_shared_managed_llm_client(profile="fast", default_priority="background") and c1._shared_managed is True
    m1 = get_shared_managed_llm_client(profile=None, model="nvidia/nemotron-3-ultra-550b-a55b:free")
    m2 = get_shared_managed_llm_client(profile=None, model="qwen/qwq-32b:free")
    assert m1 is not m2
    with c1:
        pass
    assert c1._http.is_closed is False
    close_shared_managed_llm_client()
    assert c1._http.is_closed is True and m1._http.is_closed is True and get_shared_managed_llm_client(profile="fast", default_priority="background") is not c1
