from __future__ import annotations

import ast
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse


from engraphis.core.schema import SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
README_BENCHMARK_PIN = "a78a09e312773a8d0bc87f042111ea52f4470256"
README_HOSTED_PLANS_PIN = "e440bf6ba0ff600648fdac6eb53dd28d6d80df24"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")



def test_readme_targets_resolve_in_the_repository() -> None:
    readme = _read("README.md")
    destinations = re.findall(
        r"!?\[[^\]]*\]\(([^) ]+)|(?:href|src)=\"([^\"]+)\"",
        readme,
    )
    flattened = [markdown or html for markdown, html in destinations]
    image_targets = [
        destination
        for destination in flattened
        if destination.endswith((".png", ".svg"))
    ]
    assert image_targets
    for destination in flattened:
        if destination.startswith(("#", "mailto:")):
            continue
        parsed = urlparse(destination)
        if parsed.scheme in {"http", "https"}:
            if parsed.netloc == "github.com":
                prefixes = (
                    "/Coding-Dev-Tools/engraphis/blob/fee9d0c150c250632d8e0c0ee86c1325c9e1ee78/",
                    f"/Coding-Dev-Tools/engraphis/blob/{README_BENCHMARK_PIN}/",
                    f"/Coding-Dev-Tools/engraphis/blob/{README_HOSTED_PLANS_PIN}/",
                )
            elif parsed.netloc == "raw.githubusercontent.com":
                prefixes = (
                    "/Coding-Dev-Tools/engraphis/fee9d0c150c250632d8e0c0ee86c1325c9e1ee78/",
                    f"/Coding-Dev-Tools/engraphis/{README_BENCHMARK_PIN}/",
                )
            else:
                prefixes = ()
            for prefix in prefixes:
                if parsed.path.startswith(prefix):
                    target = ROOT / unquote(parsed.path[len(prefix) :])
                    assert target.is_file(), f"README target does not exist: {destination}"
                    break
            continue
        target = destination.split("#", 1)[0]
        assert (ROOT / target).is_file(), f"README target does not exist: {destination}"

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

    for document in (_read("AGENTS.md"), _read("CLAUDE.md")):
        normalized = " ".join(document.split())
        assert "engraphis/factory.py" in normalized
        assert "outer composition root" in normalized
        assert "core/engine.py" in normalized
    readme = _read("README.md")
    assert "from engraphis.service import MemoryService" in readme
    assert "Configuration reference" in readme
    assert (
        f"[Benchmark methodology](https://github.com/Coding-Dev-Tools/engraphis/blob/{README_BENCHMARK_PIN}/"
        "BENCHMARKS.md)" in readme
    )


def test_benchmark_text_alternatives_match_registered_fixture_boundary() -> None:
    """The current image and its alt text expose only current registered boundaries."""
    from tests.test_benchmark_evidence import PUBLIC_OFFLINE_ARTIFACT

    registry_path = ROOT / "docs/benchmark-evidence" / PUBLIC_OFFLINE_ARTIFACT
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes)
    measurements = {run["id"]: run["result"] for run in registry["runs"]}
    payload = measurements["offline-performance"]
    readme = _read("README.md")
    svg_text = _read("docs/images/context-efficiency.svg")
    svg_root = ET.fromstring(svg_text)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    visible_labels = {"".join(node.itertext()).strip()
                      for node in svg_root.findall(".//svg:text", namespace)}
    prefix = hashlib.sha256(registry_bytes).hexdigest()[:12]
    assert any(prefix in label for label in visible_labels)
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

    chunking = measurements["offline-chunking"]
    grounded = measurements["offline-grounded"]
    assert "three registered offline fixtures" in alternative
    assert f"{chunking['whole']['mean_context_tokens']:.1f} to {chunking['chunked']['mean_context_tokens']:.1f} tokens" in alternative
    assert f"{payload['full_serialized_payload_tokens']:,} to {payload['compact_serialized_payload_tokens']:,} tokens" in alternative
    assert "candidate and packed retrieval quality" in alternative
    assert f"{grounded['grounded']}/{grounded['answerable']} answerable queries grounded" in alternative
    assert f"{grounded['abstained']}/{grounded['off_topic']} abstention queries rejected" in alternative
    assert "mcp transport and provider billing were not measured" in alternative
    assert hashlib.sha256(registry_bytes).hexdigest()[:12] in alternative

    benchmark_image = re.search(
        r'<img[^>]+context-efficiency\.svg[^>]*>', readme, flags=re.IGNORECASE,
    )
    assert benchmark_image is not None
    benchmark_paragraph_end = readme.find("</p>", benchmark_image.end())
    assert benchmark_paragraph_end >= 0
    caption = re.search(
        r"<sup>(.*?)</sup>", readme[benchmark_image.end():benchmark_paragraph_end],
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert caption is not None
    normalized_caption = " ".join(caption.group(1).lower().split())
    for evidence in (
        "three offline fixtures",
        "context reduction",
        "candidate and packed retrieval quality",
        "grounded behavior",
        "artifact checksum",
    ):
        assert evidence in normalized_caption

    for evidence in (
        "artifact-driven offline benchmark report",
        "structure-aware chunking",
        "retrieved candidate quality",
        "packed context quality",
        f"{payload['full_serialized_payload_tokens']:,} full",
        f"{payload['compact_serialized_payload_tokens']:,} compact",
        "mcp transport was not measured",
        "provider billing was not measured",
    ):
        assert evidence in description

    for unsupported in ("unpinned", "noncanonical", "leaderboard"):
        assert unsupported not in alternative
        assert unsupported not in description

    for retired in ("local locomo diagnostic", "3 of 15 queries", "0 of 3 to 3 of 3"):
        assert retired not in alternative
        assert retired not in description


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
    skill = _read("skills/engraphis-memory/SKILL.md")
    scoping = _read("skills/engraphis-memory/references/SCOPING.md")
    conventions = _read("skills/engraphis-memory/references/CONVENTIONS.md")
    tools = _read("skills/engraphis-memory/references/TOOLS.md")
    kilo = _read("docs/KILO_CODE_INTEGRATION.md")

    for document in (skill, scoping, tools, kilo):
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
    configuration = _read("docs/CONFIGURATION.md")
    security = _read("SECURITY.md")
    connect = _read("docs/AGENT_CONNECT.md")
    providers = _read("docs/LLM_PROVIDERS.md")
    recovery = _read("docs/RECALL_RECOVERY.md")
    sync = _read("docs/SYNC.md")

    assert (
        "[Configuration reference](https://github.com/Coding-Dev-Tools/engraphis/blob/fee9d0c150c250632d8e0c0ee86c1325c9e1ee78/"
        "docs/CONFIGURATION.md)" in readme
    )
    for document in (configuration, security, connect, providers, sync):
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

    assert f"SCHEMA_VERSION = {SCHEMA_VERSION}" in schema
    assert agents.count(f"`SCHEMA_VERSION = {SCHEMA_VERSION}`") == 2
    assert "Security policy" in readme
    assert "Cloud Sync" in readme
    assert f"schema {SCHEMA_VERSION}" in changelog

    for document in (agents, changelog, sync, erasure):
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

    assert "DOCUMENT_IMPORT.md" in readme
    for term in ("engraphis import documents", "--dry-run", "--yes"):
        assert term in guide
    for format_name in (
        "Markdown", "reStructuredText", "HTML", "JSON", "CSV", "DOCX", "ODT",
        "RTF", "XLSX", "ODS", "PPTX", "ODP", "EPUB", "Source code",
    ):
        assert format_name in guide
    for safety_term in ("symlink", "secret", "unsupported", "resumable", "temporal", "conflict"):
        assert safety_term in guide
    assert f"SCHEMA_VERSION = {SCHEMA_VERSION}" in agents
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

    assert "docs/HOSTED_PLANS.md" in readme
    hosted_plan = " ".join(_read("docs/HOSTED_PLANS.md").split())
    for public_copy in (readme, hosted_plan):
        assert "100 evaluated questions" not in public_copy
        assert "1,000 per rolling" not in public_copy
        assert "2,000 per rolling" not in public_copy
    assert "finite rolling allowance" in hosted_plan
    assert "Team usage is pooled across its active named seats" in hosted_plan
    assert "current availability and usage" in hosted_plan
    assert "currently `not_yet_available` pending release acceptance" in hosted_plan
    normalized_tools = " ".join(tools.split())
    assert "`profiles (bool, false)`; `structured (bool, false)`." in normalized_tools
