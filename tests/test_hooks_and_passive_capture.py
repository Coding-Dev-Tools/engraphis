"""Tests for Engraphis v1.8 passive capture, bounded turn-0 briefings, and client hooks."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from engraphis.core.poisoning import prompt_eligible, source_is_external
from engraphis.hooks import (
    BRIEFING_HOOK_NAME,
    CAPTURE_HOOK_NAME,
    LEGACY_CC_SESSION_KEY,
    build_session_briefing,
    extract_durable_candidates,
    extract_text_from_hook_payload,
    install_client_hooks,
    stage_passive_capture,
    uninstall_client_hooks,
)
from engraphis.hooks_cli import main as hooks_cli_main
from engraphis.service import MemoryService


def _make_service(tmp_path: Path) -> MemoryService:
    return MemoryService.create(str(tmp_path / "test.db"), embed_model=None)


def test_passive_hook_source_is_registered_external() -> None:
    assert source_is_external("passive-hook") is True


def test_extract_durable_candidates_filters_noise_and_code_blocks() -> None:
    raw = """
    Hi there! Let me check the logs.
    ```python
    # Never commit secrets inside fenced code blocks!
    api_key = "ignored"
    ```
    - Root cause: billing webhook must run before entitlement updates or seat counts drift.
    - Decided to migrate authentication tokens from JWT to PASETO v4 for local services.
    - Root cause: billing webhook must run before entitlement updates or seat counts drift.
    - Thanks, looks good!
    - Always pin npx and uvx MCP package versions to prevent silent write-tool expansion.
    """
    candidates = extract_durable_candidates(raw, max_candidates=5)
    assert len(candidates) == 3
    assert any("billing webhook" in c for c in candidates)
    assert any("PASETO v4" in c for c in candidates)
    assert any("pin npx and uvx" in c for c in candidates)


def test_extract_text_from_stop_hook_json_and_loop_guard(tmp_path: Path) -> None:
    # stop_hook_active=True must short-circuit to prevent recursive hook loops.
    text, meta = extract_text_from_hook_payload(
        json.dumps({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "Always pin versions."})
    )
    assert text == ""
    assert meta["skipped_stop_loop"] is True

    # Transcript JSONL tail extraction when last_assistant_message is absent.
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "Fix the deploy bug"}),
            json.dumps({
                "role": "assistant",
                "content": [{"type": "text", "text": "Root cause: migration script must never drop prod_audit table."}],
            }),
        ]),
        encoding="utf-8",
    )
    text2, meta2 = extract_text_from_hook_payload(
        json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript), "cwd": str(tmp_path)})
    )
    assert "migration script must never drop prod_audit" in text2
    assert meta2["format"] == "json"


def test_stage_passive_capture_rejects_secrets_and_deduplicates(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        secret_tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5"
        aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        dsn = "postgresql://admin:supersecretpass123@db.internal:5432/prod"
        prose = "\n".join([
            f"- Always set GITHUB_TOKEN={secret_tok} before running the CI release workflow.",
            f"- Never hardcode {aws_key} into terraform templates for production deploys.",
            f"- Migration root cause: {dsn} must never run against staging clusters.",
            "- Architectural decision: billing webhooks must run before seat entitlement updates.",
            "- Root cause: SQLite WAL mode requires bounded reader snapshots during backup drills.",
        ])

        first = stage_passive_capture(
            svc, prose, workspace="acme", repo="api", secret_mode="reject", max_candidates=10
        )
        assert first["skipped_secrets"] == 3
        assert first["stored_count"] == 2
        assert first["skipped_duplicates"] == 0
        assert first["review_state"] == "pending"

        # Verify stored passive captures stay pending (not prompt_eligible) and contain zero secrets.
        for mem_id in first["stored_ids"]:
            insp = svc.inspect(workspace="acme", memory_id=mem_id)
            mem = insp["memory"]
            assert mem["provenance"]["source"] == "passive-hook"
            assert mem["provenance"]["review_state"] == "pending"
            assert prompt_eligible(mem["provenance"], mem.get("metadata")) is False
            assert secret_tok not in mem["content"]
            assert aws_key not in mem["content"]

        # Running the exact same capture again must deduplicate 100% cleanly (0 new writes).
        second = stage_passive_capture(
            svc, prose, workspace="acme", repo="api", secret_mode="reject", max_candidates=10
        )
        assert second["skipped_secrets"] == 3
        assert second["skipped_duplicates"] == 2
        assert second["stored_count"] == 0
    finally:
        svc.close()


def test_stage_passive_capture_redact_mode_sanitizes_and_stores(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        tok = "sk-proj-" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        prose = f"- Security policy: never commit {tok} into repository configuration files."
        res = stage_passive_capture(svc, prose, workspace="acme", repo="api", secret_mode="redact")
        assert res["stored_count"] == 1
        assert res["skipped_secrets"] == 0
        insp = svc.inspect(workspace="acme", memory_id=res["stored_ids"][0])
        mem = insp["memory"]
        assert tok not in mem["content"]
        assert "<redacted>" in mem["content"]
    finally:
        svc.close()


def test_build_session_briefing_empty_and_scoped_queries_hide_pending(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        # Missing workspace returns clean empty briefing without raising ValidationError.
        empty = build_session_briefing(svc, workspace="nonexistent", query="")
        assert empty["count"] == 0
        assert empty["context"] == ""
        assert empty["hook_output"] == {}

        # Approved local agent decision appears; unapproved passive capture stays hidden.
        svc.remember(
            "Architectural decision: always run billing webhooks before seat entitlement updates.",
            workspace="acme",
            repo="api",
        )
        stage_passive_capture(
            svc,
            "- Root cause: unapproved passive candidate must stay hidden from turn-0 briefings.",
            workspace="acme",
            repo="api",
        )

        # Empty-query Turn-0 proactive briefing works without ValidationError.
        proactive = build_session_briefing(svc, workspace="acme", repo="api", query="", max_chars=400, client="claude")
        assert proactive["count"] >= 1
        assert len(proactive["context"]) <= 400
        assert "billing webhooks" in proactive["context"]
        assert "unapproved passive candidate" not in proactive["context"]
        assert "hookSpecificOutput" in proactive["hook_output"]

        # Scoped query briefing with Cursor output format.
        scoped = build_session_briefing(
            svc, workspace="acme", repo="api", query="billing webhooks", max_chars=300, client="cursor"
        )
        assert scoped["count"] >= 1
        assert "additional_context" in scoped["hook_output"]
        assert "billing webhooks" in scoped["hook_output"]["additional_context"]
    finally:
        svc.close()


def test_install_and_uninstall_hooks_claude_cursor_commandcode(tmp_path: Path) -> None:
    # 1. Claude Code (~/.claude/settings.json) preserves unrelated hooks & is idempotent.
    claude_cfg = tmp_path / "claude_settings.json"
    claude_cfg.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "name": "user-guard", "command": "echo ok"}]}]}}),
        encoding="utf-8",
    )
    r1 = install_client_hooks("claude", claude_cfg, python_exe="/usr/bin/python3")
    r2 = install_client_hooks("claude", claude_cfg, python_exe="/usr/bin/python3")
    assert r1["installed_events"] == ["SessionStart", "Stop"]
    assert r1["backup"] is not None and Path(r1["backup"]).exists()
    data = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert len(data["hooks"]["SessionStart"]) == 1
    assert len(data["hooks"]["Stop"]) == 1
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["name"] == "user-guard"
    cmd_start = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert ("hooks_cli.py" in cmd_start or "engraphis-hooks" in cmd_start) and " -m " not in cmd_start
    assert r2["installed_events"] == ["SessionStart", "Stop"]

    u1 = uninstall_client_hooks("claude", claude_cfg)
    assert u1["removed"] == 2
    after = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert "SessionStart" not in after["hooks"]
    assert "Stop" not in after["hooks"]
    assert after["hooks"]["PreToolUse"][0]["hooks"][0]["name"] == "user-guard"

    # 2. Cursor (.cursor/hooks.json) uses version=1 + camelCase flat lists.
    cursor_cfg = tmp_path / ".cursor" / "hooks.json"
    install_client_hooks("cursor", cursor_cfg, python_exe="/usr/bin/python3")
    cdata = json.loads(cursor_cfg.read_text(encoding="utf-8"))
    assert cdata["version"] == 1
    assert "sessionStart" in cdata["hooks"] and "stop" in cdata["hooks"]
    assert isinstance(cdata["hooks"]["sessionStart"][0], dict)
    cursor_cmd = cdata["hooks"]["sessionStart"][0]["command"]
    assert "briefing --hook-mode --client cursor" in cursor_cmd and " -m " not in cursor_cmd
    cu = uninstall_client_hooks("cursor", cursor_cfg)
    assert cu["removed"] == 2

    # 3. Command Code skips duplicate SessionStart if legacy cc-engraphis-session-start exists.
    cc_cfg = tmp_path / "cc_settings.json"
    cc_cfg.write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "name": LEGACY_CC_SESSION_KEY, "command": "old"}]}]}}),
        encoding="utf-8",
    )
    cc_res = install_client_hooks("commandcode", cc_cfg, python_exe="/usr/bin/python3")
    assert cc_res["installed_events"] == ["Stop"]
    cc_data = json.loads(cc_cfg.read_text(encoding="utf-8"))
    names_start = [e["name"] for w in cc_data["hooks"]["SessionStart"] for e in w["hooks"]]
    names_stop = [e["name"] for w in cc_data["hooks"]["Stop"] for e in w["hooks"]]
    assert names_start == [LEGACY_CC_SESSION_KEY]
    assert BRIEFING_HOOK_NAME not in names_start
    assert names_stop == [CAPTURE_HOOK_NAME]


def test_hooks_cli_briefing_never_creates_missing_db_and_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    missing_db = tmp_path / "does_not_exist.db"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = hooks_cli_main(["briefing", "--db", str(missing_db), "--hook-mode"])
    assert rc == 0
    assert not missing_db.exists()
    assert capsys.readouterr().out == ""


def test_hooks_cli_briefing_and_capture_work_without_workspace_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = tmp_path / "populated.db"
    svc = MemoryService.create(str(db_file), embed_model=None)
    try:
        svc.remember(
            "Architectural decision: never run destructive migrations without a backup.",
            workspace="default",
        )
    finally:
        svc.close()

    # Force deterministic embedder so CLI matches test DB without loading torch.
    from engraphis.config import settings
    monkeypatch.setattr(settings, "embed_model", "")

    # 1. Briefing without --workspace works in hook mode and falls back to workspace memories even for unregistered repo.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    rc1 = hooks_cli_main(["briefing", "--db", str(db_file), "--repo", "brand-new-repo", "--hook-mode"])
    assert rc1 == 0
    out1 = json.loads(capsys.readouterr().out)
    assert "never run destructive migrations" in out1["hookSpecificOutput"]["additionalContext"]

    # 2. Capture without --workspace unpacks resolve_workspace dict cleanly and stores pending candidate.
    wrapped_jsonl = tmp_path / "claude_transcript.jsonl"
    wrapped_jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Root cause: always pin MCP server package versions in ~/.claude.json."}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    stop_payload = json.dumps({"hook_event_name": "Stop", "transcript_path": str(wrapped_jsonl), "cwd": str(tmp_path)})
    monkeypatch.setattr("sys.stdin", io.StringIO(stop_payload))
    rc2 = hooks_cli_main(["capture", "--db", str(db_file), "--stdin"])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["stored_count"] == 1
    assert out2["review_state"] == "pending"


def test_control_char_secret_obfuscation_bom_and_cursor_non_list(tmp_path: Path) -> None:
    # 1. Control-char obfuscated secret is caught identically in dry_run and live capture.
    svc = _make_service(tmp_path)
    try:
        obfuscated = "ghp_\x07" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5"
        prose = f"- Security policy: always keep {obfuscated} out of git."
        dry = stage_passive_capture(svc, prose, workspace="acme", secret_mode="reject", dry_run=True)
        assert dry["skipped_secrets"] == 1
        assert dry["would_store_count"] == 0
    finally:
        svc.close()

    # 2. Windows UTF-8 BOM settings.json loads cleanly.
    bom_cfg = tmp_path / "bom_settings.json"
    bom_cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"hooks": {}}).encode("utf-8"))
    res = install_client_hooks("claude", bom_cfg, python_exe="/usr/bin/python3")
    assert res["installed_events"] == ["SessionStart", "Stop"]

    # 3. Cursor non-list event entry raises ValueError instead of clobbering dict.
    bad_cursor = tmp_path / "bad_cursor.json"
    bad_cursor.write_text(json.dumps({"version": 1, "hooks": {"sessionStart": {"custom": "keep"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        install_client_hooks("cursor", bad_cursor, python_exe="/usr/bin/python3")


def test_hook_command_resists_cwd_shadowing_and_refuses_symlinks(tmp_path: Path) -> None:
    import subprocess
    import sys

    # 1. Running the generated absolute-path hook command from a hostile repo with ./engraphis/
    #    must NEVER execute the repo-local shadowed package.
    hostile_repo = tmp_path / "hostile_repo"
    (hostile_repo / "engraphis").mkdir(parents=True)
    (hostile_repo / "engraphis" / "__init__.py").write_text("raise RuntimeError('SHADOWED_INIT')\n", encoding="utf-8")
    (hostile_repo / "engraphis" / "hooks_cli.py").write_text("raise RuntimeError('SHADOWED_CLI')\n", encoding="utf-8")

    hooks_cli_path = Path(__file__).resolve().parents[1] / "engraphis" / "hooks_cli.py"
    proc = subprocess.run(
        [sys.executable, str(hooks_cli_path), "--help"],
        cwd=str(hostile_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SHADOWED" not in proc.stdout and "SHADOWED" not in proc.stderr

    # 2. Fail-closed when provenance is None/missing in briefing candidate.
    class _StubService:
        def recall_proactive(self, *, workspace: str, repo=None, k: int = 8):
            return {"memories": [{"id": "mem_missing_prov", "content": "Unverified note without provenance."}]}

    briefing = build_session_briefing(_StubService(), workspace="acme")  # type: ignore[arg-type]
    assert briefing["count"] == 0
    assert briefing["context"] == ""

    # 3. Refuse symlinked settings/backup paths (skipped cleanly if OS denies symlink creation).
    target = tmp_path / "outside.json"
    target.write_text('{"secret": true}\n', encoding="utf-8")
    link_cfg = tmp_path / "linked_settings.json"
    try:
        link_cfg.symlink_to(target)
    except OSError:
        return
    with pytest.raises(ValueError, match="symlink"):
        install_client_hooks("claude", link_cfg, python_exe=sys.executable)




def test_briefing_respects_small_max_chars_and_query_repo_fallback(tmp_path):
    from engraphis.hooks import build_session_briefing
    from engraphis.service import MemoryService

    svc = MemoryService.create(str(tmp_path / "small_cap.db"), embed_model=None)
    try:
        svc.remember(
            "Decision: always use PASETO tokens for checkout authentication.",
            workspace="acme",
            scope="workspace",
        )
        small = build_session_briefing(svc, workspace="acme", max_chars=50)
        assert small["count"] >= 1
        assert 0 < len(small["context"]) <= 50

        zero = build_session_briefing(svc, workspace="acme", max_chars=0)
        assert zero["count"] == 0
        assert zero["context"] == ""

        # Non-empty query with unregistered repo must fall back to workspace memories.
        fb = build_session_briefing(
            svc,
            workspace="acme",
            repo="unregistered-repo",
            query="PASETO checkout tokens",
            max_chars=400,
        )
        assert fb["count"] >= 1
        assert "PASETO" in fb["context"]
    finally:
        svc.close()


def test_hooks_cli_queryless_briefing_skips_embed_model_and_handles_none_embed_dim(
    tmp_path, monkeypatch, capsys
):
    from engraphis import hooks_cli
    from engraphis.config import settings
    from engraphis.service import MemoryService

    db_file = tmp_path / "cli_fast.db"
    svc = MemoryService.create(str(db_file), embed_model=None)
    try:
        svc.remember("Convention: run ruff before pushing.", workspace="acme", scope="workspace")
    finally:
        svc.close()

    seen_calls: list[dict] = []
    real_create = MemoryService.create

    def spy_create(*args, **kwargs):
        seen_calls.append({"embed_model": kwargs.get("embed_model"), "embed_dim": kwargs.get("embed_dim")})
        return real_create(*args, **kwargs)

    monkeypatch.setattr(settings, "embed_model", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr(settings, "embed_dim", None)
    monkeypatch.setattr(MemoryService, "create", spy_create)

    rc = hooks_cli.main(["briefing", "--db", str(db_file), "--workspace", "acme", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run ruff" in out
    assert len(seen_calls) == 1
    assert seen_calls[0]["embed_model"] is None
    assert seen_calls[0]["embed_dim"] == 384
