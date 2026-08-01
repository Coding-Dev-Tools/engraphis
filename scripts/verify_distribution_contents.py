"""Verify that release archives contain the public evidence tools and no private research."""
from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Optional


REQUIRED_COMMON = frozenset({
    "eval/__init__.py",
    "eval/ablation.py",
    "eval/benchmark.py",
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
    "eval/datasets/redteam_poisoning.jsonl",
    "eval/datasets/sample.jsonl",
})
REQUIRED_SDIST = REQUIRED_COMMON | frozenset({
    "BENCHMARKS.md",
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


def _archive_names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {name.replace("\\", "/").lstrip("/") for name in archive.namelist()}
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            raw = {
                member.name.replace("\\", "/").lstrip("/")
                for member in archive.getmembers()
                if member.name
            }
        roots = {name.partition("/")[0] for name in raw}
        if len(roots) != 1:
            raise ValueError(f"{path.name}: source archive must have one root directory")
        root = next(iter(roots))
        return {
            name[len(root) + 1:] if name.startswith(f"{root}/") else ""
            for name in raw
        } - {""}
    raise ValueError(f"unsupported distribution archive: {path}")


def verify_distribution(path: Path) -> None:
    names = _archive_names(path)
    required = REQUIRED_COMMON if path.suffix == ".whl" else REQUIRED_SDIST
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
