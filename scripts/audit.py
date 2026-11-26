#!/usr/bin/env python3
"""engraphis-audit - 60-second offline proof of coding-agent memory reliability.

Runs a fast, deterministic, zero-network self-test in an isolated in-memory SQLite
database (never touching ENGRAPHIS_DB_PATH, ENGRAPHIS_DB_KEY, or ~/.engraphis/config.env):

  1. Bi-temporal supersession: confirms a superseded decision (JWT -> PASETO) is
     replaced in current recall while its historical predecessor stays inspectable.
  2. Scope isolation: confirms repo boundaries prevent cross-repository leakage.
  3. Grounded abstention: confirms an undocumented question explicitly abstains
     instead of fabricating a plausible answer from nearest neighbors.

Optionally inspects a local git repository read-only (--repo PATH) to generate a
tailored diagnostic prompt referencing a real recent commit for Claude Code,
Cursor, Codex, Cline, Zed, or Windsurf.

    engraphis-audit
    engraphis audit --repo .
    engraphis audit --markdown
    engraphis audit --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from engraphis import __version__
from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.reranker import IdentityReranker
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.secrets import redact_secrets
from engraphis.core.store import Store
from engraphis.service import MemoryService

AUDIT_URL = "https://engraphis.com/10-minute-test?utm_source=cli&utm_medium=referral&utm_campaign=ten_minute_test&utm_content=engraphis_audit"

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

AUDIT_FACTS: tuple[dict[str, Any], ...] = (
    {
        "content": "Checkout API uses JWT access tokens.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Legacy checkout token format",
        "subject_key": "auth.token_format",
        "claim_kind": "decision",
        "importance": 0.7,
    },
    {
        "content": "Checkout API uses PASETO access tokens.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Checkout token format",
        "subject_key": "auth.token_format",
        "claim_kind": "decision",
        "importance": 0.9,
    },
    {
        "content": "The November auth migration replaced JWT with PASETO to retire the legacy signer.",
        "workspace": "acme-api",
        "repo": "checkout",
        "title": "Auth migration reason",
        "subject_key": "auth.migration_reason",
        "claim_kind": "decision",
        "importance": 0.85,
    },
    {
        "content": "Stripe webhook handlers must be idempotent by event id and store processed ids before mutating billing state.",
        "workspace": "acme-api",
        "repo": "billing",
        "title": "Billing webhook idempotency",
        "subject_key": "billing.webhook_idempotency",
        "claim_kind": "convention",
        "importance": 0.9,
    },
    {
        "content": "Dashboard components use design tokens in styles/tokens.css rather than hardcoded hex colors.",
        "workspace": "acme-web",
        "repo": "dashboard",
        "title": "Dashboard styling tokens",
        "subject_key": "web.styling",
        "claim_kind": "convention",
        "importance": 0.75,
    },
)

DEFAULT_DIAGNOSTIC_PROMPT = (
    "Without guessing from surrounding code style, explain WHY this repository made "
    "[NAME ONE NON-OBVIOUS ARCHITECTURAL DECISION] and cite the exact decision record "
    "or commit where it was agreed, or state explicitly that you have no verified source."
)


def _sanitize_subject(raw: str) -> str:
    """Strip terminal control/ANSI escapes, redact secrets, and normalize dashes."""
    cleaned = _ANSI_RE.sub("", raw)
    cleaned = _CTRL_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\u2014", "-").replace("\u2013", "-")
    cleaned = redact_secrets(cleaned)
    return " ".join(cleaned.strip().split())[:140]


def _ascii_safe(text: str) -> str:
    """Ensure terminal output stays printable ASCII across Windows cp1252 and UTF-8."""
    return text.replace("\u2014", "-").replace("\u2013", "-").encode("ascii", errors="replace").decode("ascii")


def _read_recent_git_subject(repo_path: Path) -> Optional[str]:
    """Read the most recent non-merge commit subject from a local git repo (offline, read-only)."""
    target = repo_path.expanduser().resolve()
    if not target.exists() or not target.is_dir():
        return None
    env = dict(os.environ)
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                "protocol.allow=never",
                "-c",
                "log.showSignature=false",
                "-C",
                str(target),
                "log",
                "--no-show-signature",
                "--no-merges",
                "-n",
                "10",
                "--format=%s",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    lines = [_sanitize_subject(raw_line) for raw_line in proc.stdout.splitlines()]
    for subject in lines:
        if len(subject) >= 12 and not subject.lower().startswith(("bump ", "chore(deps")):
            return subject
    for subject in lines:
        if subject:
            return subject
    return None


def build_diagnostic_prompt(repo_path: Optional[Path] = None) -> tuple[str, Optional[str]]:
    """Build a copyable agent diagnostic prompt, optionally anchored to a real commit."""
    if repo_path is None:
        return DEFAULT_DIAGNOSTIC_PROMPT, None
    subject = _read_recent_git_subject(repo_path)
    if not subject:
        return DEFAULT_DIAGNOSTIC_PROMPT, None
    quoted_subject = json.dumps(subject)
    prompt = (
        f"Without guessing from surrounding code style, explain WHY this repository made "
        f"the architectural decision behind the commit subject (untrusted text) {quoted_subject} "
        f"and cite the exact decision record or commit where it was agreed, or state explicitly "
        f"that you have no verified source."
    )
    return prompt, subject


def _create_isolated_service() -> MemoryService:
    """Build a hermetically isolated offline MemoryService without touching host env/config."""
    store = Store(":memory:", connect=None, sqlite_durability="balanced")
    try:
        embedder = DeterministicEmbedder(384)
        index = NumpyVectorIndex(store, dim=embedder.dim)
        reranker = IdentityReranker()
        engine = MemoryEngine(
            store,
            embedder,
            index,
            reranker,
            auto_evolve=True,
            extractor=None,
            auto_extractor_factory=None,
            graph_extractor=None,
            graph_feeder=None,
            retention_supervisor=None,
            allow_automatic_critical_retention=False,
        )
        return MemoryService(engine)
    except BaseException:
        store.close()
        raise


def run_audit(*, repo_path: Optional[Path] = None) -> dict[str, Any]:
    """Execute the 3-check offline memory audit in an isolated in-memory SQLite DB."""
    started = time.perf_counter()
    service = _create_isolated_service()
    try:
        base_ts = time.time() - 3600.0
        for idx, fact in enumerate(AUDIT_FACTS):
            service.remember(**fact, valid_from=base_ts + idx * 60.0)

        # Check 1: Bi-temporal supersession (JWT -> PASETO)
        why_res = service.why(
            "checkout access token format",
            workspace="acme-api",
            repo="checkout",
            k=5,
        )
        answer_items = why_res.get("answer", [])
        active_texts = [str(item.get("content") or "") for item in answer_items]
        superseded_texts = [
            str(item.get("content") or "") for item in why_res.get("supersedes", [])
        ]
        has_paseto = any(
            str(item.get("content") or "").strip() == "Checkout API uses PASETO access tokens."
            or (
                item.get("subject_key") == "auth.token_format"
                and "PASETO" in str(item.get("content") or "")
            )
            for item in answer_items
        )
        has_jwt_superseded = any("JWT" in text for text in superseded_texts)
        no_live_jwt_only = not any(
            text.strip() == "Checkout API uses JWT access tokens." for text in active_texts
        )
        check1_passed = bool(has_paseto and has_jwt_superseded and no_live_jwt_only)

        # Check 2: Scope isolation (billing vs checkout vs dashboard)
        scope_res = service.recall(
            "Stripe webhook idempotency",
            workspace="acme-api",
            repo="checkout",
            k=5,
        )
        leaked_billing = any(
            "Stripe webhook" in str(item.get("content") or "")
            for item in scope_res.get("memories", [])
        )
        billing_res = service.recall(
            "Stripe webhook idempotency",
            workspace="acme-api",
            repo="billing",
            k=5,
        )
        found_in_billing = any(
            "Stripe webhook" in str(item.get("content") or "")
            for item in billing_res.get("memories", [])
        )
        web_res = service.recall(
            "Stripe webhook idempotency",
            workspace="acme-web",
            repo="dashboard",
            k=5,
        )
        leaked_to_web = any(
            "Stripe webhook" in str(item.get("content") or "")
            for item in web_res.get("memories", [])
        )
        check2_passed = bool((not leaked_billing) and (not leaked_to_web) and found_in_billing)

        # Check 3: Grounded abstention on an undocumented architectural choice
        abstain_res = service.grounded_recall(
            "Why did we select Apache Kafka for order notification queues?",
            workspace="acme-api",
            repo="checkout",
            k=5,
        )
        grounded_flag = bool(abstain_res.get("grounded", False))
        citations = list(abstain_res.get("citations") or [])
        check3_passed = (not grounded_flag) and (len(citations) == 0)
    finally:
        service.close()

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    prompt, commit_subject = build_diagnostic_prompt(repo_path)

    checks = [
        {
            "id": "bitemporal_supersession",
            "title": "Bi-temporal supersession (JWT -> PASETO)",
            "passed": check1_passed,
            "without_engraphis": "Flat context keeps both JWT and PASETO chunks with no validity state.",
            "with_engraphis": (
                "Returned active PASETO decision and preserved closed JWT record in supersedes history."
                if check1_passed
                else "Failed to separate active PASETO record from superseded JWT history."
            ),
        },
        {
            "id": "scope_isolation",
            "title": "Repository scope isolation (checkout vs billing)",
            "passed": check2_passed,
            "without_engraphis": "Flat vector store leaks billing webhook rules into checkout queries.",
            "with_engraphis": (
                "Zero billing leakage in checkout scope; exact recall preserved in billing scope."
                if check2_passed
                else "Scope boundary leaked across repositories or missed target scope."
            ),
        },
        {
            "id": "grounded_abstention",
            "title": "Grounded abstention on undocumented decision",
            "passed": check3_passed,
            "without_engraphis": "Agent fabricates a plausible reason for Kafka from nearest neighbors.",
            "with_engraphis": (
                "Explicitly abstained (grounded=false, 0 citations) instead of guessing."
                if check3_passed
                else "Returned an ungrounded answer instead of abstaining."
            ),
        },
    ]

    passed_count = sum(1 for item in checks if item["passed"])
    return {
        "schema": "engraphis-audit/v1",
        "version": __version__,
        "offline": True,
        "elapsed_ms": elapsed_ms,
        "passed": passed_count == len(checks),
        "score": f"{passed_count}/{len(checks)}",
        "checks": checks,
        "repo_commit_hint": commit_subject,
        "diagnostic_prompt": prompt,
        "test_url": AUDIT_URL,
    }


def render_terminal(report: dict[str, Any]) -> str:
    """Format a compact, ASCII-safe terminal scorecard."""
    lines = [
        f"Engraphis Offline Memory Audit v{report['version']} ({report['elapsed_ms']} ms, isolated SQLite)",
        "=" * 74,
    ]
    for check in report["checks"]:
        badge = "[PASS]" if check["passed"] else "[FAIL]"
        lines.append(f"{badge} {check['title']}")
        lines.append(f"       Before (flat context): {check['without_engraphis']}")
        lines.append(f"       After  (Engraphis):    {check['with_engraphis']}")
    lines.extend(
        [
            "-" * 74,
            f"Score: {report['score']} checks passed (offline NumPy core, zero network calls).",
            "",
            "Paste this into a fresh Claude Code / Cursor / Codex / Cline / Zed / Windsurf session:",
            f"  {report['diagnostic_prompt']}",
            "",
            "Next steps:",
            '  1. Install MCP server: python -m pip install "engraphis[mcp]"',
            "  2. Initialize config:  engraphis-init",
            "  3. Pre-cache weights:  engraphis-init --prefetch",
            "  4. Full 4-step guide:  " + str(report["test_url"]),
        ]
    )
    return _ascii_safe("\n".join(lines))


def render_markdown(report: dict[str, Any]) -> str:
    """Format a shareable Markdown scorecard (strictly zero emdashes)."""
    lines = [
        f"### Engraphis Offline Memory Audit ({report['score']} passed in {report['elapsed_ms']} ms)",
        "",
        "| Check | Result | Engraphis Behavior |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        lines.append(f"| {check['title']} | {status} | {check['with_engraphis']} |")
    lines.extend(
        [
            "",
            "**Agent diagnostic prompt:**",
            f"> {report['diagnostic_prompt']}",
            "",
            f"Run it locally: `uvx --from engraphis engraphis-audit` | [10-Minute Agent Memory Test]({report['test_url']})",
        ]
    )
    return "\n".join(lines).replace("\u2014", "-")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engraphis-audit" if argv is not None else None,
        description="Run a 60-second offline coding-agent memory audit in an isolated SQLite DB.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Optional local git repository path (read-only) to tailor the agent diagnostic prompt.",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--json",
        action="store_true",
        help="Output the structured audit report as JSON.",
    )
    fmt.add_argument(
        "--markdown",
        action="store_true",
        help="Output a compact shareable Markdown scorecard.",
    )
    args = parser.parse_args(argv)

    report = run_audit(repo_path=args.repo)
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.markdown:
        print(render_markdown(report))
    else:
        print(render_terminal(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
