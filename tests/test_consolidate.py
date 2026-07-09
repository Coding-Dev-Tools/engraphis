import time

from engraphis.core.consolidate import consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, SearchFilter


def _engine_with_repeats():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    texts = [
        "Build failed on the flaky network integration test in CI run 101.",
        "Build failed on the flaky network integration test in CI run 202.",
        "Build failed on the flaky network integration test in CI run 303.",
        "Design review scheduled for the onboarding flow mockups.",   # unrelated
    ]
    for t in texts:
        eng.remember(t, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return eng, wid, rid


def test_consolidate_distills_recurring_episodes_into_semantic_digest():
    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["clusters_found"] == 1
    assert len(report["digests_created"]) == 1
    digest_id = report["digests_created"][0]["id"]
    digest = eng.store.get_memory(digest_id)
    assert digest.mtype == MemoryType.SEMANTIC
    assert "flaky" in digest.content or "network" in digest.content
    assert digest.metadata["provenance"]["source"] == "consolidation"
    links = eng.store.get_links(digest_id)
    assert sum(1 for link in links if link["relation"] == "consolidates") == 3


def test_consolidate_is_idempotent():
    eng, wid, rid = _engine_with_repeats()
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    second = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert len(first["digests_created"]) == 1
    assert len(second["digests_created"]) == 0
    assert second["skipped_already_consolidated"] >= 1


def test_consolidate_dry_run_changes_nothing():
    eng, wid, rid = _engine_with_repeats()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert report["dry_run"] is True
    assert before == after
    assert report["digests_created"] and "would_consolidate" in report["digests_created"][0]


def test_consolidate_archives_decayed_transients_but_not_pinned():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    stale = eng.remember("Currently blocked on CI quota.", workspace_id=wid, repo_id=rid,
                         mtype=MemoryType.WORKING)
    pinned = eng.remember("Blocked on the vendor contract renewal.", workspace_id=wid,
                          repo_id=rid, mtype=MemoryType.WORKING)
    eng.pin(pinned)
    # Age both far past any plausible retention: tiny stability, ancient last_access.
    old = time.time() - 90 * 86400
    for mid in (stale, pinned):
        eng.store.conn.execute(
            "UPDATE memories SET stability=0.5, last_access=? WHERE id=?", (old, mid))
    eng.store.conn.commit()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    archived_ids = {a["id"] for a in report["archived"]}
    assert stale in archived_ids
    assert pinned not in archived_ids
    live = {m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100)}
    assert stale not in live                     # left the live view...
    assert eng.store.get_memory(stale) is not None   # ...but never hard-deleted
    assert pinned in live


def test_consolidate_uses_llm_summary_when_available():
    class FakeLLM:
        def chat(self, messages, system=None, **kw):
            return "CI is flaky on the network integration test; treat failures as retryable."

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=FakeLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.content.startswith("CI is flaky")


def test_consolidate_llm_failure_falls_back_to_deterministic():
    class BrokenLLM:
        def chat(self, messages, system=None, **kw):
            raise RuntimeError("provider down")

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=BrokenLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert "Recurring pattern" in digest.content


# ── compaction token-accounting (made a number) ───────

def _engine_with_large_cluster(n: int = 12):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for i in range(n):
        eng.remember(
            f"Nightly deploy to staging failed because the migration lock timed out, run {i}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    return eng, wid, rid


def test_consolidate_reports_compaction_savings_on_a_real_cluster():
    eng, wid, rid = _engine_with_large_cluster()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    comp = report["compaction"]["distilled"]
    assert comp["tokens_before"] > comp["tokens_after"] > 0
    assert comp["tokens_saved"] == comp["tokens_before"] - comp["tokens_after"]
    assert 0 < comp["reduction_pct"] <= 100
    # every digest entry carries its own before/after so the report is auditable
    entry = report["digests_created"][0]
    for key in ("tokens_before", "tokens_after", "tokens_saved", "reduction_pct"):
        assert key in entry
    assert report["compaction"]["total_tokens_saved"] >= comp["tokens_saved"]


def test_consolidate_archive_reports_freed_tokens():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("Temporary: blocked on CI quota until the weekend.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.WORKING)
    old = time.time() - 90 * 86400
    eng.store.conn.execute("UPDATE memories SET stability=0.5, last_access=? WHERE id=?",
                           (old, mid))
    eng.store.conn.commit()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["archived"] and report["archived"][0]["tokens_freed"] > 0
    assert report["compaction"]["archived_tokens_freed"] >= report["archived"][0]["tokens_freed"]


def test_consolidate_dry_run_reports_compaction_without_writing():
    eng, wid, rid = _engine_with_large_cluster()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert before == after                                   # nothing written
    assert report["compaction"]["distilled"]["tokens_saved"] > 0   # but savings estimated


# ── entity Profiles pass (a "profile that grows with you") ──────────

def _engine_with_entity_mentions(name: str = "Aurora", n: int = 8):
    from engraphis.core.interfaces import Node
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    lines = [
        f"{name} prefers PASETO over JWT because key rotation was painful last quarter.",
        f"{name} approved raising the API rate limit to 500 requests per minute.",
        f"{name} owns the billing-service migration and wants it done before the freeze.",
        f"{name} dislikes force-pushes to shared branches on the deploy repo.",
        f"{name} asked that all new endpoints ship with contract tests attached.",
        f"{name} reviewed the incident and blamed the migration lock timeout.",
        f"{name} set the staging deploy window to weekday evenings only.",
        f"{name} keeps the on-call runbook in the ops workspace, not the wiki.",
    ][:n]
    for t in lines:
        eng.remember(t, workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
                     resolve_conflicts=False)
    eng.store.upsert_entity(Node(id="", name=name, ntype="person", workspace_id=wid, repo_id=rid))
    return eng, wid, rid, name


def test_profiles_pass_rolls_entity_memories_into_one_digest():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, name = _engine_with_entity_mentions()
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert report["entities_considered"] == 1
    assert len(report["profiles_created"]) == 1
    entry = report["profiles_created"][0]
    assert entry["entity"] == name and entry["mentions"] == 8
    prof = eng.store.get_memory(entry["id"])
    assert prof.mtype == MemoryType.SEMANTIC
    assert prof.title == f"Profile: {name}"
    assert prof.metadata["provenance"]["source"] == "profile_consolidation"
    links = eng.store.get_links(entry["id"])
    assert sum(1 for link in links if link["relation"] == "profiles") == 8
    assert report["compaction"]["tokens_before"] > report["compaction"]["tokens_after"] > 0


def test_profiles_pass_via_consolidate_flag_and_is_idempotent():
    eng, wid, rid, _ = _engine_with_entity_mentions()
    first = consolidate(eng, workspace_id=wid, repo_id=rid, profiles=True)
    second = consolidate(eng, workspace_id=wid, repo_id=rid, profiles=True)
    assert len(first["profiles"]["profiles_created"]) == 1
    assert len(second["profiles"]["profiles_created"]) == 0
    assert second["profiles"]["skipped_existing"] >= 1


def test_profiles_pass_respects_min_mentions():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, _ = _engine_with_entity_mentions(name="Rare", n=2)
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert report["profiles_created"] == []


def test_profiles_dry_run_changes_nothing():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, _ = _engine_with_entity_mentions()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert before == after
    assert report["profiles_created"] and "would_profile" in report["profiles_created"][0]


# ── scheduled report artifact (scripts/consolidate.py --report, Team-gated) ──────────

import pytest  # noqa: E402

from engraphis import licensing as _lic  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from scripts.consolidate import main as consolidate_main  # noqa: E402

_SECRET = bytes(range(32))


@pytest.fixture()
def _license_env(monkeypatch):
    """Free tier by default; returns a helper that installs a signed key."""
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    _lic.current_license(refresh=True)

    def _use(plan):
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", compose_key(
            {"v": 1, "plan": plan, "email": "t@x.co", "seats": 3,
             "issued": int(time.time()), "expires": None}, _SECRET))
        _lic.current_license(refresh=True)
    yield _use
    _lic.current_license(refresh=True)


def _seed_db(tmp_path):
    db = tmp_path / "mem.db"
    eng = MemoryEngine.create(str(db))
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for i in range(3):
        eng.remember(f"Build failed on the flaky network test in CI run {i}.",
                     workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return db


def test_report_flag_is_team_gated_and_fails_before_touching_the_db(
        _license_env, tmp_path, capsys):
    db = _seed_db(tmp_path)
    out = tmp_path / "report.md"
    rc = consolidate_main(["--db", str(db), "--workspace", "w", "--report", str(out)])
    assert rc == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "Team feature" in err and "https://" in err     # actionable upsell, not a crash

    # a Pro key is not enough — reports are the Team ops artifact
    _license_env("pro")
    assert consolidate_main(
        ["--db", str(db), "--workspace", "w", "--report", str(out)]) == 2
    assert not out.exists()


def test_report_flag_writes_markdown_summary_with_before_after_counts(
        _license_env, tmp_path, capsys):
    _license_env("team")
    db = _seed_db(tmp_path)
    out = tmp_path / "reports" / "consolidation.md"       # parent dir auto-created
    rc = consolidate_main(["--db", str(db), "--workspace", "w", "--report", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Engraphis consolidation report")
    assert "**workspace:** w" in text
    assert "**live memories before:** 3" in text
    assert "**live memories after:**" in text
    assert "**digests created:** 1" in text               # the cluster got merged
    assert "**memories merged into digests:** 3" in text
    assert "transients archived" in text and "generated" in text
    assert capsys.readouterr().out.strip().startswith("{")   # JSON still on stdout


def test_report_flag_renders_html_when_extension_says_so(_license_env, tmp_path):
    _license_env("team")
    db = _seed_db(tmp_path)
    out = tmp_path / "consolidation.html"
    assert consolidate_main(
        ["--db", str(db), "--workspace", "w", "--dry-run", "--report", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Engraphis consolidation report" in page
    assert "dry run — nothing changed" in page
    assert "<script" not in page and "src=" not in page   # self-contained here too


def test_sweep_without_report_needs_no_license(_license_env, tmp_path):
    db = _seed_db(tmp_path)   # free tier from the fixture default
    assert consolidate_main(["--db", str(db), "--workspace", "w"]) == 0
