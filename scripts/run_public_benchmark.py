"""Plan and execute a locked, local-only public benchmark run.

This module is deliberately an orchestration boundary.  It does not resolve
datasets or models, download anything, publish anything, or accept arbitrary
commands from a manifest.  A manifest only selects one of the repository's
allowlisted runners and supplies immutable local provenance.

Dry-run is the default.  ``--execute`` is required before any subprocess is
started, and execution is forced into offline Hugging Face/Transformers mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "engraphis-public-benchmark-manifest/v1"
RUNNERS = {"harness"}
FORMATS = {"jsonl"}
CANONICAL_BUDGETS = [256, 512, 1024, 2048, 4096]
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class ManifestError(ValueError):
    """Raised when a manifest cannot safely define a benchmark run."""


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(f"non-finite JSON value is not allowed: {value}")
            ),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest must be a JSON object")
    return value


def _required_object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ManifestError(f"{name} must not contain NUL")
    return value


def _immutable(value: Any, name: str) -> str:
    value = _required_string(value, name)
    if not _SHA1.fullmatch(value):
        raise ManifestError(f"{name} must be a lowercase 40-character commit")
    return value


def _digest(value: Any, name: str) -> str:
    value = _required_string(value, name)
    if not _SHA256.fullmatch(value):
        raise ManifestError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _local_path(value: Any, name: str) -> str:
    value = _required_string(value, name)
    if "://" in value or value.startswith(("http:", "https:", "git:")):
        raise ManifestError(f"{name} must be a local path, not a URL")
    return value


def _relative_output(value: Any, name: str) -> str:
    value = _local_path(value, name)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{name} must be a relative output path without '..'")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a copy of the locked manifest.

    Unknown keys are rejected so a future mutable option cannot silently enter
    the execution protocol without a corresponding review and test.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    allowed = {
        "schema", "locked", "run_id", "runner", "benchmark", "dataset",
        "source", "models", "repo", "config", "outputs",
    }
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        raise ManifestError("unknown manifest fields: " + ", ".join(unknown))
    if manifest.get("schema") != SCHEMA:
        raise ManifestError(f"schema must equal {SCHEMA}")
    if manifest.get("locked") is not True:
        raise ManifestError("locked must be true")
    run_id = _required_string(manifest.get("run_id"), "run_id")
    if not _RUN_ID.fullmatch(run_id):
        raise ManifestError("run_id contains unsupported characters")
    runner = manifest.get("runner")
    if runner not in RUNNERS:
        raise ManifestError("runner must be one of: " + ", ".join(sorted(RUNNERS)))

    benchmark = _required_object(manifest.get("benchmark"), "benchmark")
    if set(benchmark) != {"name", "format"}:
        raise ManifestError("benchmark must contain exactly name and format")
    _required_string(benchmark.get("name"), "benchmark.name")
    benchmark_format = _required_string(benchmark.get("format"), "benchmark.format")
    if benchmark_format not in FORMATS:
        raise ManifestError("benchmark.format is unsupported")
    if runner == "harness" and benchmark_format != "jsonl":
        raise ManifestError("harness runner requires benchmark.format=jsonl")

    dataset = _required_object(manifest.get("dataset"), "dataset")
    if set(dataset) != {"path", "sha256", "revision"}:
        raise ManifestError("dataset must contain exactly path, sha256, and revision")
    _local_path(dataset.get("path"), "dataset.path")
    _digest(dataset.get("sha256"), "dataset.sha256")
    _immutable(dataset.get("revision"), "dataset.revision")

    source = _required_object(manifest.get("source"), "source")
    if set(source) != {"repository", "revision"}:
        raise ManifestError("source must contain exactly repository and revision")
    _required_string(source.get("repository"), "source.repository")
    _immutable(source.get("revision"), "source.revision")

    models = _required_object(manifest.get("models"), "models")
    if set(models) != {"embedding", "reader"}:
        raise ManifestError("models must contain exactly embedding and reader")
    for model_kind in ("embedding", "reader"):
        item = _required_object(models.get(model_kind), f"models.{model_kind}")
        if set(item) != {"model", "revision"}:
            raise ManifestError(
                f"models.{model_kind} must contain exactly model and revision"
            )
        _required_string(item.get("model"), f"models.{model_kind}.model")
        _immutable(item.get("revision"), f"models.{model_kind}.revision")

    repo = _required_object(manifest.get("repo"), "repo")
    if set(repo) != {"root", "commit"}:
        raise ManifestError("repo must contain exactly root and commit")
    _local_path(repo.get("root"), "repo.root")
    _immutable(repo.get("commit"), "repo.commit")

    config = _required_object(manifest.get("config"), "config")
    if set(config) != {"baseline_label", "token_budgets", "k", "canonical_profile"}:
        raise ManifestError(
            "config must contain exactly baseline_label, token_budgets, k, and canonical_profile"
        )
    _required_string(config.get("baseline_label"), "config.baseline_label")
    if config.get("token_budgets") != CANONICAL_BUDGETS:
        raise ManifestError("config.token_budgets must equal the canonical fixed budgets")
    if not isinstance(config.get("k"), int) or isinstance(config.get("k"), bool) or config["k"] <= 0:
        raise ManifestError("config.k must be a positive integer")
    _local_path(config.get("canonical_profile"), "config.canonical_profile")

    outputs = _required_object(manifest.get("outputs"), "outputs")
    if set(outputs) != {"directory", "report", "artifact", "claims"}:
        raise ManifestError("outputs must contain exactly directory, report, artifact, and claims")
    _local_path(outputs.get("directory"), "outputs.directory")
    output_names = {
        name: _relative_output(outputs.get(name), f"outputs.{name}")
        for name in ("report", "artifact", "claims")
    }
    if len(set(output_names.values())) != len(output_names):
        raise ManifestError("outputs.report, artifact, and claims must be distinct")

    return json.loads(json.dumps(manifest, sort_keys=True, allow_nan=False))


def load_manifest(path: str | Path) -> dict[str, Any]:
    return validate_manifest(_load_json(Path(path)))


def _resolve(root: Path, value: str) -> str:
    return str((root / value).resolve()) if not Path(value).is_absolute() else str(Path(value).resolve())


def _redact_command(command: Sequence[str], substitutions: Mapping[str, str]) -> list[str]:
    return [substitutions.get(value, value) for value in command]


def build_plan(manifest: Mapping[str, Any], *, python: str | None = None) -> dict[str, Any]:
    """Build a JSON-safe command plan without checking out or reading datasets."""
    config = validate_manifest(manifest)
    root = Path(config["repo"]["root"]).resolve()
    output_dir = Path(_resolve(root, config["outputs"]["directory"]))
    dataset = _resolve(root, config["dataset"]["path"])
    profile = _resolve(root, config["config"]["canonical_profile"])
    report = str(output_dir / config["outputs"]["report"])
    artifact = str(output_dir / config["outputs"]["artifact"])
    claims = str(output_dir / config["outputs"]["claims"])
    executable = python or sys.executable
    common = {
        dataset: "<locked-dataset>",
        profile: "<locked-profile>",
        report: "<private-report>",
        artifact: "<public-artifact>",
        claims: "<public-claims>",
    }
    commands: list[dict[str, Any]] = []
    benchmark_command = [
        executable, "-m", "eval.harness", "--dataset", dataset,
        "--k", str(config["config"]["k"]), "--v2", "--canonical",
        "--canonical-profile", profile, "--baseline-label",
        config["config"]["baseline_label"], "--artifact", artifact,
    ]
    commands.append({"kind": "benchmark", "command": benchmark_command})
    commands.append({
        "kind": "claim_validation",
        "command": [executable, "-m", "eval.public_readiness", "--artifact", artifact,
                     "--claims", claims],
    })
    for item in commands:
        item["cwd"] = str(root)
        item["redacted_command"] = _redact_command(item["command"], common)
    return {
        "schema": "engraphis-public-benchmark-plan/v1",
        "run_id": config["run_id"],
        "dry_run": True,
        "execute_required": True,
        "network_policy": "offline_assets_only",
        "publication": "not performed by this CLI",
        "commands": commands,
        "outputs": {"directory": str(output_dir), "report": report,
                    "artifact": artifact, "claims": claims},
    }


def _verify_execute_inputs(config: Mapping[str, Any]) -> None:
    root = Path(config["repo"]["root"]).resolve()
    if not root.is_dir():
        raise ManifestError("repo.root does not exist or is not a directory")
    dataset = Path(_resolve(root, config["dataset"]["path"]))
    if not dataset.is_file():
        raise ManifestError("dataset.path does not exist; refusing to download it")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    if digest != config["dataset"]["sha256"]:
        raise ManifestError("dataset.sha256 does not match local dataset bytes")
    profile = Path(_resolve(root, config["config"]["canonical_profile"]))
    if not profile.is_file():
        raise ManifestError("config.canonical_profile does not exist")
    profile_value = _load_json(profile)
    expected_profile = {
        ("benchmark", "repository"): config["source"]["repository"],
        ("benchmark", "repository_revision"): config["source"]["revision"],
        ("benchmark", "dataset_revision"): config["dataset"]["revision"],
        ("embedding", "model"): config["models"]["embedding"]["model"],
        ("embedding", "revision"): config["models"]["embedding"]["revision"],
        ("reader", "model"): config["models"]["reader"]["model"],
        ("reader", "revision"): config["models"]["reader"]["revision"],
    }
    for (section, field), expected_value in expected_profile.items():
        section_value = profile_value.get(section)
        actual_value = (
            section_value.get(field) if isinstance(section_value, Mapping) else None
        )
        if actual_value != expected_value:
            raise ManifestError(
                f"canonical profile {section}.{field} does not match the locked manifest"
            )
    if profile_value.get("baseline_label") != config["config"]["baseline_label"]:
        raise ManifestError(
            "canonical profile baseline_label does not match the locked manifest"
        )
    if profile_value.get("token_budgets") != config["config"]["token_budgets"]:
        raise ManifestError(
            "canonical profile token_budgets do not match the locked manifest"
        )
    expected = config["repo"]["commit"]
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestError("could not verify repo.commit") from exc
    if actual != expected:
        raise ManifestError("repo.commit does not match the current checkout")
    if status:
        raise ManifestError("repo.root must be a clean worktree")


def _stage_claims_input(source: Path, destination: Path) -> None:
    """Snapshot a pre-reviewed claims file without replacing prior run state."""
    if source.is_symlink() or not source.is_file():
        raise ManifestError("claims_input must be a regular non-symlink JSON file")
    try:
        payload = source.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read claims_input: {exc}") from exc
    if not isinstance(value, list) and not (
        isinstance(value, Mapping) and isinstance(value.get("claims"), list)
    ):
        raise ManifestError("claims_input must be a JSON array or an object with a claims array")
    try:
        if source.resolve() == destination.resolve():
            raise ManifestError("claims_input must not be the claims output")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ManifestError("claims output must not be a symlink")
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != payload:
                raise ManifestError(f"refusing to replace staged claims: {destination}")
            return
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise ManifestError(f"could not stage claims_input: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def execute_plan(plan: Mapping[str, Any], manifest: Mapping[str, Any], *,
                 claims_input: Path | None = None,
                 runner: Callable[..., Any] = subprocess.run) -> None:
    """Execute only a previously built plan after explicit input verification."""
    config = validate_manifest(manifest)
    expected = build_plan(config)
    if plan.get("schema") != expected["schema"] or plan.get("run_id") != expected["run_id"]:
        raise ManifestError("plan identity does not match the locked manifest")
    if plan.get("commands") != expected["commands"]:
        raise ManifestError("plan commands do not match the locked manifest")
    _verify_execute_inputs(config)
    if claims_input is None:
        raise ManifestError("claims_input is required for execution")
    _stage_claims_input(claims_input, Path(expected["outputs"]["claims"]))
    environment = os.environ.copy()
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    for item in plan.get("commands", []):
        command = item.get("command")
        if not isinstance(command, list) or not command:
            raise ManifestError("plan contains an invalid command")
        runner(command, cwd=item["cwd"], check=True, env=environment)


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    if path.exists() and path.read_bytes() != payload:
        raise ManifestError(f"refusing to replace immutable plan: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or execute a locked public benchmark run.")
    parser.add_argument("--manifest", required=True, help="locked JSON manifest")
    parser.add_argument("--plan-output", help="write the command plan to this JSON path")
    parser.add_argument("--execute", action="store_true", help="explicitly permit subprocesses")
    parser.add_argument(
        "--claims-input",
        help="protected pre-reviewed public claims JSON staged before benchmark execution",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        plan = build_plan(manifest)
        if args.plan_output:
            _write_immutable_json(Path(args.plan_output), plan)
        if args.execute:
            if not args.claims_input:
                raise ManifestError("--execute requires --claims-input")
            execute_plan(plan, manifest, claims_input=Path(args.claims_input))
        elif args.claims_input:
            raise ManifestError("--claims-input requires --execute")
        else:
            print(json.dumps(plan, sort_keys=True, indent=2))
            print("dry-run only: pass --execute to run the allowlisted subprocess plan", file=sys.stderr)
    except (ManifestError, OSError, subprocess.CalledProcessError) as exc:
        print(f"public benchmark run rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
