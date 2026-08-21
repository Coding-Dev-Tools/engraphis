from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")



def test_readme_long_description_uses_no_repository_relative_targets() -> None:
    readme = _read("README.md")
    destinations = re.findall(
        r"!?\[[^\]]*\]\(([^) ]+)|(?:href|src)=\"([^\"]+)\"",
        readme,
    )
    flattened = [markdown or html for markdown, html in destinations]
    relative = [
        destination
        for destination in flattened
        if not destination.startswith(("#", "https://", "http://"))
    ]
    assert not relative

    image_targets = [
        destination
        for destination in flattened
        if destination.endswith((".png", ".svg"))
    ]
    assert image_targets
    assert all(
        target.startswith(
            "https://raw.githubusercontent.com/Coding-Dev-Tools/engraphis/main/"
        )
        or target.startswith("https://img.shields.io/")
        for target in image_targets
    )

def test_canonical_offline_gate_tracks_ci() -> None:
    agents = _read("AGENTS.md")
    claude = _read("CLAUDE.md")
    workflow = _read(".github/workflows/ci.yml")
    required = (
        "ruff check .",
        "python scripts/check_commercial_manifest.py",
        "python scripts/externalize_dashboard_assets.py",
        "python -m pytest",
        "python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5",
        "python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5",
        "python -m eval.ablation",
        "python -m eval.reinforcement",
        "python -m eval.adversarial_memory_security",
        "python -m eval.grounded",
        "python -m eval.code_arm",
        "pyright",
    )

    for command in required:
        assert command in agents, f"AGENTS.md omits the canonical gate command: {command}"
        assert command in workflow, f"CI omits the documented gate command: {command}"

    assert "Use the exact primary offline gate in `AGENTS.md` §1" in claude
    assert "do not maintain a smaller duplicate here" in claude


def test_core_backend_imports_stay_behind_outer_composition_root() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "engraphis" / "core").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module.startswith("engraphis.backends"):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not violations, violations

    factory = ast.parse(_read("engraphis/factory.py"), filename="engraphis/factory.py")
    backend_modules = {
        node.module
        for node in ast.walk(factory)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("engraphis.backends")
    }
    assert backend_modules, "outer composition root no longer imports concrete backends"
    package = _read("engraphis/__init__.py")
    assert "configure_engine_factory(_default_memory_engine_factory)" in package
    assert "create_memory_engine" in package

    for document in (_read("AGENTS.md"), _read("CLAUDE.md"), _read("README.md")):
        normalized = " ".join(document.split())
        assert "engraphis/factory.py" in normalized
        assert "outer composition root" in normalized
        assert "core/engine.py" in normalized


def test_benchmark_text_alternatives_match_registered_fixture_boundary() -> None:
    readme = _read("README.md")
    svg_text = _read("docs/images/context-efficiency.svg")
    svg_root = ET.fromstring(svg_text)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    description_node = svg_root.find("svg:desc", namespace)
    assert description_node is not None
    description = " ".join("".join(description_node.itertext()).lower().split())
    image = re.search(
        r'<img[^>]+context-efficiency\.svg[^>]+alt="([^"]+)"',
        readme,
        flags=re.IGNORECASE,
    )
    assert image is not None
    alternative = " ".join(image.group(1).lower().split())

    for evidence in (
        "local measurements and deterministic fixtures",
        "local locomo diagnostic",
        "740.3 to 214.3 tokens",
        "162.2 to 42.4 tokens",
        "3 of 15 queries",
        "15 of 15",
        "0 of 3 to 3 of 3",
        "2 of 2 summary cases",
        "10,202 rather than 23,810 tokens",
        "10 of 10 correct decisions",
        "85.38 tokens under a 1,500-token cap",
    ):
        assert evidence in alternative
        assert evidence in description

    assert "not an mcp transport" in description
    assert "does not measure provider billing" in description
    assert "1,500-token cap" in description
    for unsupported in ("unpinned", "noncanonical", "leaderboard"):
        assert unsupported not in alternative
        assert unsupported not in description


def test_official_longmemeval_runbook_tracks_attested_evidence_contract() -> None:
    benchmarks = _read("BENCHMARKS.md")
    runbook = _read("docs/PUBLIC_BENCHMARK_RUNBOOK.md")
    normalized_benchmarks = " ".join(benchmarks.split())
    normalized_runbook = " ".join(runbook.split())

    for value in (
        "balanced",
        "planner",
        "episodic_cap_2",
        "planner_episodic_cap_2",
        "context_k_2",
        "planner_context_k_2",
    ):
        assert value in runbook
    assert "30 official runs" in runbook
    assert "six declared variants at all five token budgets" in normalized_benchmarks
    assert "context_k=2" in runbook

    for option in (
        "--engraphis-execution-manifest",
        "--engraphis-per-question",
        "--engraphis-questions",
        "--engraphis-haystack",
        "--engraphis-trajectories",
        "--engraphis-memory-config",
        "--engraphis-matrix-manifest",
        "--engraphis-seed",
        "--execution-manifest",
        "--claims-input",
    ):
        assert option in runbook
    assert "set equality between every source question ID and output question ID" in runbook
    assert "only after a successful return" in normalized_benchmarks.lower()
    assert "inserted and retrieved counts by memory type" in normalized_runbook
    assert "at least two inserted memory types" in normalized_runbook

    assert "does not publish per-record content fingerprints" in normalized_benchmarks
    assert "whole-input/source-file digests" in normalized_runbook
    assert "no raw questions, answers, prompts, context" in normalized_runbook
    assert "no per-record content hashes or fingerprints" in normalized_runbook


def test_scope_and_event_guidance_match_fail_closed_runtime_contract() -> None:
    readme = _read("README.md")
    skill = _read("skills/engraphis-memory/SKILL.md")
    scoping = _read("skills/engraphis-memory/references/SCOPING.md")
    conventions = _read("skills/engraphis-memory/references/CONVENTIONS.md")
    tools = _read("skills/engraphis-memory/references/TOOLS.md")
    kilo = _read("docs/KILO_CODE_INTEGRATION.md")

    for document in (readme, skill, scoping, tools, kilo):
        normalized = " ".join(document.split())
        assert "reserved and rejected" in normalized
        assert "owner identity" in normalized

    for document in (conventions, tools):
        normalized = " ".join(document.lower().split())
        assert "event rows are not memories" in normalized
        assert "not recalled" in normalized
        assert "not" in normalized and "consolidated" in normalized

    assert 'mtype="episodic"' in conventions
    assert "≤0.2" in conventions


def test_configuration_and_recovery_guidance_matches_public_contracts() -> None:
    readme = _read("README.md")
    security = _read("SECURITY.md")
    connect = _read("docs/AGENT_CONNECT.md")
    providers = _read("docs/LLM_PROVIDERS.md")
    recovery = _read("docs/RECALL_RECOVERY.md")
    sync = _read("docs/SYNC.md")

    for document in (readme, security, connect, providers, sync):
        normalized = " ".join(document.split())
        assert "~/.engraphis/config.env" in normalized
        assert "ENGRAPHIS_ENV_FILE" in normalized
        assert re.search(r"(?:never|does not) search(?:es)? the working directory", normalized)

    assert "repaired_fields" in recovery
    assert "v1_memory_id" in recovery
    assert "v1_thought_id" in recovery
    assert "v1_document_id" in recovery
    assert "first contact" in sync
    assert "incomplete" in sync
    assert "unanchored" in sync
    assert "--relay-token" in sync and "--relay-e2ee-key" in sync
    assert "intentionally has no secret-valued" in sync



def test_schema_and_erasure_docs_match_live_export_policy() -> None:
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    changelog = _read("CHANGELOG.md")
    sync = _read("docs/SYNC.md")
    erasure = _read("docs/SECURE_ERASURE.md")
    schema = _read("engraphis/core/schema.py")

    assert "SCHEMA_VERSION = 16" in schema
    assert agents.count("`SCHEMA_VERSION = 16`") == 2
    assert "schema 16" in readme
    assert "schema 16" in changelog

    for document in (agents, readme, changelog, sync, erasure):
        normalized = " ".join(document.split())
        assert "never_export" in normalized
        assert "remote_erasure" in normalized

    normalized_sync = " ".join(sync.split())
    assert "only `remote_erasure`" in normalized_sync
    assert "never leave the device" in normalized_sync
    assert "cannot later be upgraded" in normalized_sync
    assert "only a non-secret workspace/repo record" in erasure


def test_document_import_docs_describe_the_source_neutral_contract() -> None:
    readme = _read("README.md")
    agents = _read("AGENTS.md")
    guide = _read("docs/DOCUMENT_IMPORT.md")
    obsidian = _read("docs/OBSIDIAN_IMPORT.md")

    for document in (readme, guide):
        assert "engraphis import documents" in document
        assert "--dry-run" in document
        assert "--yes" in document
    for format_name in (
        "Markdown", "reStructuredText", "HTML", "JSON", "CSV", "DOCX", "ODT",
        "RTF", "XLSX", "ODS", "PPTX", "ODP", "EPUB", "Source code",
    ):
        assert format_name in guide
    for safety_term in ("symlink", "secret", "unsupported", "resumable", "temporal", "conflict"):
        assert safety_term in guide
    assert "SCHEMA_VERSION = 16" in agents
    assert "source-neutral" in agents
    assert "rich Markdown adapter" in obsidian
    assert "DOCUMENT_IMPORT.md" in obsidian


def test_consolidation_docs_expose_only_live_public_options() -> None:
    readme = _read("README.md")
    tools = _read("skills/engraphis-memory/references/TOOLS.md")
    changelog = _read("CHANGELOG.md")

    for document in (readme, tools, changelog):
        assert "supersede_sources" not in document
        assert "supersede-sources" not in document

    assert "source episodes remain live" in readme
    normalized_tools = " ".join(tools.split())
    assert "`profiles (bool, false)`; `structured (bool, false)`." in normalized_tools
