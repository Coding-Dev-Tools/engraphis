"""Offline tests for the MemoryService facade (numpy-only, no model download, no mcp).

Covers the validated write/read path the MCP server delegates to: round-trip recall,
scope isolation, session lifecycle, input validation, and untrusted-content sanitization
(the memory-poisoning guard), plus conflict resolution, governance, and the bi-temporal
why/timeline/proactive tools.
"""
import pytest

from engraphis.service import MemoryService, ValidationError


def _svc() -> MemoryService:
    return MemoryService.create(":memory:")


def test_remember_then_recall_roundtrip():
    s = _svc()
    out = s.remember("We use pnpm as the package manager for all frontend repos.",
                     workspace="acme", repo="web")
    assert out["stored"] is True
    assert out["id"].startswith("mem_")
    assert out["scope"] == "repo" and out["mtype"] == "semantic"

    r = s.recall("which package manager for the frontend?", workspace="acme", repo="web")
    assert r["count"] >= 1
    assert "pnpm" in r["context"]
    assert any("pnpm" in m["content"] for m in r["memories"])


def test_scope_isolation_by_workspace():
    s = _svc()
    s.remember("Secret alpha fact about widgets.", workspace="alpha")
    s.remember("Secret beta fact about gadgets.", workspace="beta")
    r = s.recall("fact", workspace="alpha")
    assert r["count"] >= 1
    assert all(m["content"] != "Secret beta fact about gadgets." for m in r["memories"])


def test_recall_unknown_workspace_is_empty_not_error():
    s = _svc()
    r = s.recall("anything", workspace="does-not-exist")
    assert r["count"] == 0
    assert "note" in r


def test_session_lifecycle():
    s = _svc()
    started = s.start_session("acme", repo="web", agent="claude-code", goal="ship auth")
    sid = started["session_id"]
    assert sid.startswith("ses_") and started["status"] == "active"

    s.remember("Decided to use PASETO over JWT.", workspace="acme", repo="web",
               session_id=sid, mtype="episodic")
    ended = s.end_session(sid, summary="Auth migrated to PASETO.", outcome="shipped")
    assert ended["status"] == "summarized"

    with pytest.raises(ValidationError):
        s.end_session("ses_does_not_exist")


def test_stats_counts():
    s = _svc()
    s.remember("one", workspace="acme", mtype="semantic")
    s.remember("two", workspace="acme", mtype="procedural")
    st = s.stats(workspace="acme")
    assert st["memories"] == 2
    assert st["by_type"].get("procedural") == 1
    assert st["schema_version"] >= 2


@pytest.mark.parametrize("kwargs", [
    {"content": "", "workspace": "acme"},                       # empty content
    {"content": "x", "workspace": ""},                          # empty workspace
    {"content": "x", "workspace": "bad;name"},                  # illegal name char
    {"content": "x", "workspace": "acme", "mtype": "bogus"},    # bad enum
    {"content": "x", "workspace": "acme", "scope": "bogus"},    # bad enum
    {"content": "x" * 100_001, "workspace": "acme"},            # oversized content
])
def test_remember_validation_rejects_bad_input(kwargs):
    s = _svc()
    with pytest.raises(ValidationError):
        s.remember(**kwargs)


def test_control_characters_are_stripped():
    s = _svc()
    out = s.remember("hello\x00\x07world", workspace="acme")  # NUL + BEL injected
    rec = s.store.get_memory(out["id"])
    assert "\x00" not in rec.content and "\x07" not in rec.content
    assert rec.content == "helloworld"


def test_importance_is_clamped():
    s = _svc()
    out = s.remember("important", workspace="acme", importance=9.0)
    rec = s.store.get_memory(out["id"])
    assert rec.importance == 1.0


def test_provenance_recorded():
    s = _svc()
    out = s.remember("traceable fact", workspace="acme", source="unit-test")
    rec = s.store.get_memory(out["id"])
    assert rec.metadata.get("provenance", {}).get("source") == "unit-test"


# ── conflict resolution on the write path ───────────────────────────────────────

def test_remember_reports_add_op():
    s = _svc()
    out = s.remember("We use pnpm.", workspace="acme", repo="web")
    assert out["op"] == "add"


def test_remember_noop_on_duplicate_reports_op():
    s = _svc()
    text = "We standardized on pnpm as the package manager for all frontend repos."
    s.remember(text, workspace="acme", repo="web")
    out = s.remember(text, workspace="acme", repo="web")
    assert out["op"] == "noop"
    assert "resolution" in out


def test_remember_invalidate_reports_superseded():
    s = _svc()
    first = s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                       workspace="acme", repo="web")
    second = s.remember(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace="acme", repo="web")
    assert second["op"] == "invalidate"
    assert second["superseded"] == [first["id"]]


def test_remember_resolve_conflicts_false_keeps_both():
    s = _svc()
    text = "Build failed again."
    a = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    b = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    assert a["op"] == "add" and b["op"] == "add" and a["id"] != b["id"]


# ── session continuity (cross-session handoff) ───────────────────────────────────

def test_start_session_bootstraps_from_prior_session():
    s = _svc()
    first = s.start_session("acme", repo="web", goal="refactor auth")
    assert first["bootstrap"] == {}
    s.end_session(first["session_id"], summary="mid-refactor", outcome="blocked",
                  open_threads=["tests 3-5 failing"])
    second = s.start_session("acme", repo="web", goal="finish refactor")
    assert second["bootstrap"]["summary"] == "mid-refactor"
    assert second["bootstrap"]["open_threads"] == ["tests 3-5 failing"]
    assert second["bootstrap"]["outcome"] == "blocked"


# ── governance: forget / pin / correct ──────────────────────────────────────────

def test_forget_then_recall_excludes_it():
    s = _svc()
    out = s.remember("A fact to forget.", workspace="acme", repo="web")
    s.forget(out["id"], workspace="acme", repo="web", reason="no longer relevant")
    r = s.recall("fact to forget", workspace="acme", repo="web")
    assert all(m["id"] != out["id"] for m in r["memories"])


def test_forget_unknown_id_raises_validation_error():
    s = _svc()
    s.remember("anchor", workspace="acme")   # workspace must exist for _require_scope
    with pytest.raises(ValidationError):
        s.forget("mem_does_not_exist", workspace="acme")


def test_forget_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.forget(out["id"], workspace="beta")          # beta doesn't own alpha's memory
    r = s.recall("private fact", workspace="alpha")
    assert any(m["id"] == out["id"] for m in r["memories"])   # untouched


def test_pin_roundtrip():
    s = _svc()
    out = s.remember("Pin me.", workspace="acme")
    pinned = s.pin(out["id"], workspace="acme")
    assert pinned["pinned"] is True
    unpinned = s.pin(out["id"], workspace="acme", pinned=False)
    assert unpinned["pinned"] is False


def test_pin_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.pin(out["id"], workspace="beta")


def test_correct_supersedes():
    s = _svc()
    out = s.remember("The API key header is X-Auth-Key.", workspace="acme")
    corrected = s.correct(out["id"], "The API key header is X-Api-Key.", workspace="acme",
                          reason="typo")
    assert corrected["superseded"] == [out["id"]]
    r = s.recall("API key header", workspace="acme")
    assert any("X-Api-Key" in m["content"] for m in r["memories"])


def test_correct_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.correct(out["id"], "tampered", workspace="beta")


# ── bi-temporal: why / timeline / recall_proactive ───────────────────────────────

def test_why_returns_answer_and_history():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.why("what is the rate limit", workspace="acme", repo="web")
    assert any("500" in m["content"] for m in out["answer"])
    assert any("100" in m["content"] for m in out["supersedes"])


def test_why_unknown_workspace_raises():
    s = _svc()
    with pytest.raises(ValidationError):
        s.why("anything", workspace="does-not-exist")


def test_timeline_orders_chronologically():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.timeline("rate limit", workspace="acme", repo="web")
    assert len(out["history"]) == 2
    assert out["history"][0]["valid_from"] <= out["history"][1]["valid_from"]


def test_recall_proactive_includes_last_session():
    s = _svc()
    s.remember("High importance convention.", workspace="acme", repo="web", importance=0.9)
    started = s.start_session("acme", repo="web")
    s.end_session(started["session_id"], summary="mid-work", open_threads=["thing left undone"])
    out = s.recall_proactive(workspace="acme", repo="web")
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["thing left undone"]


# ── linking & events ─────────────────────────────────────────────────────────────

def test_record_event_and_link():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme", repo="web")
    b = s.remember("Memory B.", workspace="acme", repo="web")
    ev = s.record_event("decision", "Chose PASETO over JWT.", workspace="acme", repo="web")
    assert ev["id"].startswith("evt_")
    link = s.link(a["id"], b["id"], workspace="acme", repo="web", relation="related")
    assert link["linked"] is True


def test_link_unknown_id_raises():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme")
    with pytest.raises(ValidationError):
        s.link(a["id"], "mem_nope", workspace="acme")


def test_link_wrong_workspace_raises_validation_error():
    s = _svc()
    a = s.remember("Alpha's fact.", workspace="alpha")
    b = s.remember("Beta's fact.", workspace="beta")
    with pytest.raises(ValidationError):
        s.link(a["id"], b["id"], workspace="alpha")    # b isn't alpha's to link


# ── code-symbol graph ─────────────────────────────────────────────────────────────

def test_index_repo_and_search_code(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )
    s = _svc()
    report = s.index_repo(workspace="acme", repo="sample", root_path=str(tmp_path))
    assert report["files_indexed"] == 1
    out = s.search_code("add", workspace="acme", repo="sample")
    assert any(sym["name"] == "add" for sym in out["symbols"])


def test_search_code_requires_repo():
    s = _svc()
    s.remember("x", workspace="acme")
    with pytest.raises(ValidationError):
        s.search_code("add", workspace="acme", repo="")
