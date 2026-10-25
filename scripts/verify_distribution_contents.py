"""Verify that release archives contain the public evidence tools and no private research."""
from __future__ import annotations

import argparse
import re
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Optional


REQUIRED_COMMON = frozenset({
    "integrations/commandcode/session_start_hook.py",
    "integrations/commandcode/file_guard_hook.py",
    "scripts/templates/benchmark_examples.svg",
    "engraphis/core/mutations.py",
    "engraphis/core/vector_repair.py",
    "engraphis/dashboard_assets/action-dialog.js",
    "engraphis/dashboard_assets/ask-requests.js",
    "engraphis/dashboard_assets/engraphis-graph-every-worker.js",
    "engraphis/dashboard_assets/engraphis-graph-every.js",
    "engraphis/dashboard_assets/engraphis-graph.js",
    "engraphis/dashboard_assets/engraphis-spacetime.js",
    "engraphis/dashboard_assets/graph-explorer.js",
    "engraphis/dashboard_assets/graph-lifecycle.js",
    "engraphis/dashboard_assets/import-sources.js",
    "engraphis/dashboard_assets/index.html",
    "engraphis/dashboard_assets/ledger.css",
    "engraphis/dashboard_assets/ledger.js",
    "engraphis/dashboard_assets/managed-processing.js",
    "engraphis/dashboard_assets/markdown.js",
    "engraphis/dashboard_assets/memory-history.js",
    "engraphis/dashboard_assets/memory-revision.js",
    "engraphis/dashboard_assets/memory-workflow.css",
    # The only shipped copies of the graph libraries; Explore cannot render without them.
    "engraphis/dashboard_assets/vendor/d3.LICENSE",
    "engraphis/dashboard_assets/vendor/d3.min.js",
    "engraphis/dashboard_assets/vendor/force-graph.LICENSE",
    "engraphis/dashboard_assets/vendor/force-graph.min.js",
    "engraphis/dashboard_assets/workflow-context.js",
    "scripts/smoke_entry_points.py",
    "eval/__init__.py",
    "eval/ablation.py",
    "eval/benchmark.py",
    "eval/capacity_matrix.py",
    "eval/coding_acceptance.py",
    "eval/engine_capacity.py",
    "eval/rework_statistics.py",
    "eval/task_pairs.py",
    "eval/chunking_eval.py",
    "eval/external.py",
    "eval/grounded.py",
    "eval/harness.py",
    "eval/longmemeval_v2.py",
    "eval/metrics.py",
    "eval/performance.py",
    "eval/redteam_poisoning.py",
    "eval/run_longmemeval_v2.py",
    "eval/configs/longmemeval_v2_engraphis.json",
    "eval/datasets/adversarial.jsonl",
    "eval/datasets/codemem.jsonl",
    "eval/datasets/graph_multihop.jsonl",
    "eval/datasets/longdoc.jsonl",
    "eval/datasets/locomo10_repair_manifest.json",
    "eval/datasets/redteam_poisoning.jsonl",
    "eval/datasets/sample.jsonl",
})
REQUIRED_SDIST = REQUIRED_COMMON | frozenset({
    "docs/REWORK_EXECUTION.md",
    "docs/ENGINE_CAPACITY_PROTOCOL.md",
    "docs/CODING_ACCEPTANCE_CORPUS.md",
    "docs/PAID_EVALUATION_PROPOSAL.md",
    "BENCHMARKS.md",
    "docs/DOCUMENT_IMPORT.md",
    "docs/OBSIDIAN_IMPORT.md",
    "docker-compose.lan.yml",
    "deploy/force-graph-1.51.4.licenses.json",
    "deploy/force-graph-1.51.4.yarn.lock",
    "eval/BASELINES.md",
})
_PRIVATE_RESEARCH = (
    re.compile(r"internal.*material", re.IGNORECASE),
    re.compile(r"private.*research", re.IGNORECASE),
    re.compile(r"commercial.*audit", re.IGNORECASE),
    re.compile(r"competitive.*analysis", re.IGNORECASE),
    re.compile(r"competitor.*research", re.IGNORECASE),
    re.compile(r"market.*research", re.IGNORECASE),
    re.compile(r"pricing.*research", re.IGNORECASE),
)

# Runtime artifacts and private material that must never ship in a public
# distribution.  A published wheel that carries a credential, a customer
# database, a log file, a source-control directory, or the test suite is a
# publication incident, so the release gate has to reject it rather than
# notice it later.
_FORBIDDEN_NAMES = frozenset({
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ds_store",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})
_FORBIDDEN_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
)
_FORBIDDEN_DIRECTORIES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    ".eggs",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
})
# ``.env.example`` is the documented template and belongs in the source
# distribution; every other ``.env`` file holds real values.
_ALLOWED_ENV_TEMPLATES = frozenset({".env.example"})


def _forbidden_reason(name: str, *, wheel: bool) -> Optional[str]:
    """Return why a member may not ship, or ``None`` when it is acceptable."""
    parts = name.casefold().split("/")
    base = parts[-1]
    if any(part in _FORBIDDEN_DIRECTORIES for part in parts[:-1]):
        return "a version-control or tool-cache directory"
    if wheel and parts[0] == "tests":
        return "the test suite"
    if base in _FORBIDDEN_NAMES:
        return "a private configuration or credential file"
    if base.startswith(".env.") and base not in _ALLOWED_ENV_TEMPLATES:
        return "a private environment file"
    if base.endswith(_FORBIDDEN_SUFFIXES):
        return "a credential, database, or log file"
    return None


def _archive_names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = set()
            for info in archive.infolist():
                name = info.filename
                folded = name.replace("\\", "/")
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    not folded
                    or folded.startswith("/")
                    or re.match(r"^[A-Za-z]:", folded)
                    or ".." in folded.split("/")
                ):
                    raise ValueError(f"{path.name}: wheel member has unsafe path: {name!r}")
                if mode == stat.S_IFLNK:
                    raise ValueError(f"{path.name}: wheel member must not be a symlink: {name!r}")
                normalized = folded
                if normalized in names:
                    raise ValueError(f"{path.name}: wheel contains duplicate member: {name!r}")
                names.add(normalized)
            return names
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            raw = set()
            for member in archive.getmembers():
                folded = member.name.replace("\\", "/")
                # Validate before stripping anything: an absolute path such as
                # "/engraphis-1.0.0/file" must not become apparently relative.
                if (
                    not folded
                    or folded.startswith("/")
                    or re.match(r"^[A-Za-z]:", folded)
                    or ".." in folded.split("/")
                ):
                    raise ValueError(
                        f"{path.name}: source member has unsafe path: {member.name!r}"
                    )
                if not (member.isfile() or member.isdir()):
                    raise ValueError(
                        f"{path.name}: source member must be a regular file or directory: "
                        f"{member.name!r}"
                    )
                if folded in raw:
                    raise ValueError(
                        f"{path.name}: source archive contains duplicate member: "
                        f"{member.name!r}"
                    )
                raw.add(folded)
        roots = {name.partition("/")[0] for name in raw}
        if len(roots) != 1:
            raise ValueError(f"{path.name}: source archive must have one root directory")
        root = next(iter(roots))
        if not re.fullmatch(r"[A-Za-z0-9._-]+", root):
            raise ValueError(f"{path.name}: source archive root is unsafe: {root!r}")
        return {
            name[len(root) + 1:] if name.startswith(f"{root}/") else ""
            for name in raw
        } - {""}
    raise ValueError(f"unsupported distribution archive: {path}")


def verify_distribution(path: Path) -> None:
    names = _archive_names(path)
    wheel = path.suffix == ".whl"
    required = REQUIRED_COMMON if wheel else REQUIRED_SDIST
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"{path.name}: missing required files: {', '.join(missing)}")

    unsafe = []
    for name in names:
        folded = name.casefold()
        searchable = re.sub(r"[-_/]+", " ", folded)
        if (
            "__pycache__" in folded
            or folded.endswith((".pyc", ".pyo"))
            or any(pattern.search(searchable) for pattern in _PRIVATE_RESEARCH)
        ):
            unsafe.append(name)
    for name in sorted(names):
        reason = _forbidden_reason(name, wheel=wheel)
        if reason is not None:
            unsafe.append(f"{name} ({reason})")
    if unsafe:
        raise ValueError(
            f"{path.name}: private or generated files present: {', '.join(sorted(unsafe))}"
        )


def verify_distributions(paths: Iterable[Path]) -> None:
    archives = [Path(path) for path in paths]
    wheels = [path for path in archives if path.suffix == ".whl"]
    sdists = [path for path in archives if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one .tar.gz source distribution")
    for path in (*wheels, *sdists):
        verify_distribution(path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_distributions(args.archives)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print("distribution contents verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
