"""Safe continuation runner for the frozen OAuth coding pilot."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional

from eval import benchmark_campaign as campaign
from eval.benchmark import canonical_json, report_envelope, sha256_file, write_canonical_artifact
from eval.campaign_api import LunaResponsesClient
from eval.campaign_ledger import BudgetApproval, CampaignBinding, CampaignLedger
from eval.coding_corpus import DATASET_ROOT, load_corpus


CONTINUATION_SCHEMA = "engraphis-campaign-continuation/v1"
ELIGIBILITY_SCHEMA = "engraphis-campaign-eligibility/v1"
INVALID_FIXTURE_REASON = "invalid_fixture_exact_wording"
DEFAULT_TIMEOUT_SECONDS = 180
CONTINUATION_TIMEOUT_SECONDS = 600
CHILD_MAX_CALLS = 180
CHILD_MAX_COST_MICROS = 2359440
PARENT_TERMINAL_CHECKPOINTS = 59
PARENT_TERMINAL_CALLS = 82
PARENT_COMPLETED_CALLS = 81
PARENT_UNCERTAIN_CALLS = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ContinuationError(ValueError):
    """A fail-closed continuation validation or execution stop."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuationError(f"cannot read artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ContinuationError(f"artifact must be an object: {path}")
    return value


def _save_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical_json(dict(value)) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ContinuationError(f"durable artifact already exists: {path}") from exc
    except OSError as exc:
        raise ContinuationError(f"cannot write artifact: {path}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _cell_key(cell: Mapping[str, Any]) -> str:
    return canonical_json(dict(cell))


def _question_id(cell: Mapping[str, Any]) -> str:
    return f"{cell['scenario_id']}:{cell['arm']}:{cell['token_budget']}:{cell['repetition']}"


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContinuationError(f"{field} is not a SHA-256 digest")
    return value


def _cells(manifest: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    try:
        result = campaign.cells(dict(manifest), stage)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationError("manifest stage cells are invalid") from exc
    if len({_cell_key(item) for item in result}) != len(result):
        raise ContinuationError("manifest contains duplicate cells")
    return result


def validate_eligibility(
    manifest: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    audit_artifact_path: Optional[Path] = None,
) -> tuple[dict[str, Any], ...]:
    """Require the exact 15-cell whole-scenario retrospective exclusion mask."""
    value = dict(artifact)
    if value.get("schema") != ELIGIBILITY_SCHEMA:
        raise ContinuationError("eligibility schema mismatch")
    if value.get("parent_campaign_sha256") != manifest.get("binding_sha256"):
        raise ContinuationError("eligibility parent binding mismatch")
    if value.get("stage") != "development_pilot" or value.get("origin") != "implementation_team":
        raise ContinuationError("eligibility stage or origin mismatch")
    if value.get("retrospective") is not True:
        raise ContinuationError("eligibility must be retrospective")
    audit_hash = _require_sha(value.get("audit_artifact_sha256"), "audit_artifact_sha256")
    if audit_artifact_path is not None and sha256_file(audit_artifact_path) != audit_hash:
        raise ContinuationError("corpus audit artifact changed")
    supplied = value.get("binding_sha256")
    unsigned = {key: item for key, item in value.items() if key != "binding_sha256"}
    if supplied != _digest(unsigned):
        raise ContinuationError("eligibility binding checksum mismatch")
    expected = [
        cell for cell in _cells(manifest, "development_pilot")
        if cell["scenario_id"] == "atlas-north:long_documents"
    ]
    expected_keys = {_cell_key(cell) for cell in expected}
    exclusions = value.get("exclusions")
    if not isinstance(exclusions, list):
        raise ContinuationError("eligibility exclusions are not an array")
    observed: dict[str, dict[str, Any]] = {}
    for item in exclusions:
        if not isinstance(item, dict):
            raise ContinuationError("eligibility exclusion is not an object")
        cell = {key: item.get(key) for key in ("scenario_id", "arm", "token_budget", "repetition")}
        if any(item is None for item in cell.values()) or item.get("reason") != INVALID_FIXTURE_REASON:
            raise ContinuationError("eligibility exclusion cell or reason is invalid")
        if set(item) != set(cell) | {"reason"}:
            raise ContinuationError("eligibility exclusion has unbound fields")
        key = _cell_key(cell)
        if key in observed:
            raise ContinuationError("eligibility exclusion is duplicated")
        observed[key] = cell
    if len(observed) != 15 or set(observed) != expected_keys:
        raise ContinuationError("eligibility must cover all 15 long_documents cells")
    return tuple(observed[key] for key in sorted(observed))


def eligible_missing_cells(
    manifest: Mapping[str, Any],
    stage: str,
    existing_cells: set[str] | Mapping[str, Any],
    excluded_cells: set[str] | Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    expected = _cells(manifest, stage)
    expected_keys = {_cell_key(cell) for cell in expected}
    existing = set(existing_cells)
    excluded = set(excluded_cells)
    if not existing <= expected_keys or not excluded <= expected_keys:
        raise ContinuationError("cell mask contains a cell outside the stage")
    missing = [cell for cell in expected if _cell_key(cell) not in existing]
    return (
        tuple(cell for cell in missing if _cell_key(cell) not in excluded),
        tuple(cell for cell in missing if _cell_key(cell) in excluded),
    )


def _binding(manifest: Mapping[str, Any], stage: str) -> CampaignBinding:
    try:
        return CampaignBinding(
            campaign_id=f"{manifest['campaign_id']}-{stage}",
            model=manifest["model"], reasoning_effort=manifest["reasoning_effort"],
            dataset_sha256=manifest["corpus"]["manifest_sha256"],
            config_sha256=manifest["binding_sha256"],
            repo_revision=manifest["repository_revision"], pins_sha256=manifest["pins_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationError("manifest cannot form a campaign binding") from exc


def derive_child_approval(
    parent_approval: BudgetApproval,
    *,
    parent_calls: int,
    parent_reserved_cost_micros: int,
    max_calls: int = CHILD_MAX_CALLS,
    max_cost_micros: int = CHILD_MAX_COST_MICROS,
) -> BudgetApproval:
    """Derive a ledger cap from the parent allowance; never writes an approval."""
    if (isinstance(parent_calls, bool) or not isinstance(parent_calls, int) or parent_calls < 0
            or isinstance(parent_reserved_cost_micros, bool)
            or not isinstance(parent_reserved_cost_micros, int)
            or parent_reserved_cost_micros < 0):
        raise ContinuationError("parent ledger totals are invalid")
    if (max_calls != CHILD_MAX_CALLS or max_cost_micros != CHILD_MAX_COST_MICROS
            or max_calls > parent_approval.max_calls - parent_calls
            or max_cost_micros > parent_approval.max_cost_micros - parent_reserved_cost_micros):
        raise ContinuationError("child cap exceeds the parent remaining allowance")
    return BudgetApproval.create(
        max_calls=max_calls, max_cost_micros=max_cost_micros,
        input_micros_per_million=parent_approval.input_micros_per_million,
        cached_input_micros_per_million=parent_approval.cached_input_micros_per_million,
        cache_write_micros_per_million=parent_approval.cache_write_micros_per_million,
        output_micros_per_million=parent_approval.output_micros_per_million,
    )


def _checkpoint_digest(directory: Path) -> str:
    if not directory.is_dir():
        raise ContinuationError(f"checkpoint directory missing: {directory}")
    if list(directory.glob("*.started")):
        raise ContinuationError("unfinished checkpoint reservation requires reconciliation")
    files = sorted(directory.glob("*.json"), key=lambda path: path.name)
    return _digest([{"name": path.name, "sha256": sha256_file(path)} for path in files])


def _load_checkpoints(
    directory: Path,
    binding_sha256: str,
    expected: Mapping[str, Mapping[str, Any]],
    *,
    expected_count: Optional[int] = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    checkpoint_hash = _checkpoint_digest(directory)
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            checkpoint = _read(path)
            cell, row = checkpoint["cell"], checkpoint["row"]
            if (checkpoint.get("binding_sha256") != binding_sha256
                    or not isinstance(cell, dict) or not isinstance(row, dict)):
                raise ContinuationError("checkpoint binding or shape mismatch")
            key = _cell_key(cell)
            if key not in expected or dict(expected[key]) != cell:
                raise ContinuationError("checkpoint cell is outside the frozen stage")
            if path.name != f"{campaign.digest(cell)}.json":
                raise ContinuationError("checkpoint filename is not cell-bound")
            if checkpoint.get("row_sha256") != _digest(row):
                raise ContinuationError("checkpoint row checksum mismatch")
            campaign.validate_row(row, cell)
            if key in rows:
                raise ContinuationError("checkpoint cell is duplicated")
            rows[key] = row
        except ContinuationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ContinuationError(f"invalid checkpoint: {path.name}") from exc
    if expected_count is not None and len(rows) != expected_count:
        raise ContinuationError(f"expected {expected_count} checkpoints, got {len(rows)}")
    return rows, checkpoint_hash


def _validate_public(path: Path, binding_sha256: str, expected: Mapping[str, Any]) -> None:
    report = _read(path)
    sidecar_path = Path(str(path) + ".sha256")
    if not sidecar_path.is_file():
        raise ContinuationError("parent public report checksum sidecar is missing")
    try:
        sidecar = sidecar_path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        raise ContinuationError("parent public report checksum sidecar is unreadable") from exc
    if sidecar != sha256_file(path):
        raise ContinuationError("parent public report checksum mismatch")
    metrics, protocol = report.get("metrics"), report.get("protocol", {})
    if (not isinstance(metrics, dict) or not isinstance(protocol, dict)
            or metrics.get("campaign_sha256") != binding_sha256
            or protocol.get("config", {}).get("campaign_sha256") != binding_sha256
            or metrics.get("stage") != "development_pilot"
            or metrics.get("expected_attempts") != 150
            or metrics.get("missing_attempts") != 91
            or metrics.get("statuses") != {"complete": 58, "error": 1}):
        raise ContinuationError("parent public report is not the frozen 59-row artifact")
    records = report.get("records")
    if not isinstance(records, list) or len(records) != PARENT_TERMINAL_CHECKPOINTS:
        raise ContinuationError("parent public report row count changed")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ContinuationError("parent public row is malformed")
        cell = {key: record.get(key) for key in ("scenario_id", "arm", "token_budget", "repetition")}
        key = _cell_key(cell)
        if key not in expected or key in seen:
            raise ContinuationError("parent public row cell mismatch")
        seen.add(key)


def _validate_parent_approval(
    artifact: Mapping[str, Any], manifest: Mapping[str, Any], stage: str, ledger_path: Path,
) -> BudgetApproval:
    if (artifact.get("schema") != "engraphis-campaign-stage-approval/v1"
            or artifact.get("campaign_sha256") != manifest.get("binding_sha256")
            or artifact.get("stage") != stage
            or artifact.get("ledger_path_sha256") != _digest(str(ledger_path.resolve()))):
        raise ContinuationError("parent approval does not bind the original ledger")
    try:
        approved_at = datetime.fromisoformat(str(artifact["approved_at"]))
        expires_at = datetime.fromisoformat(str(artifact["expires_at"]))
        now = datetime.now(timezone.utc)
        if (approved_at.tzinfo is None or expires_at.tzinfo is None
                or not approved_at <= now < expires_at):
            raise ValueError("approval window is not active")
        return BudgetApproval.from_artifact(artifact["approval"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationError("parent approval is invalid or expired") from exc


def _child_manifest(
    parent: Mapping[str, Any], eligibility: Mapping[str, Any], *,
    parent_manifest_sha256: str, parent_checkpoint_sha256: str, parent_ledger_sha256: str,
) -> dict[str, Any]:
    child = deepcopy(dict(parent))
    child["campaign_id"] = f"{parent['campaign_id']}-continuation"
    child["repository_revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=campaign.ROOT, text=True
    ).strip()
    child["oauth"] = dict(parent["oauth"])
    child["oauth"]["attempt_timeout_seconds"] = CONTINUATION_TIMEOUT_SECONDS
    child["source"] = campaign.source_snapshot()
    child["continuation_source"] = {
        "eval/campaign_continuation.py": sha256_file(Path(__file__).resolve())
    }
    child.update({
        "continuation_parent_campaign_sha256": parent["binding_sha256"],
        "continuation_parent_manifest_sha256": parent_manifest_sha256,
        "continuation_parent_checkpoint_sha256": parent_checkpoint_sha256,
        "continuation_parent_ledger_sha256": parent_ledger_sha256,
        "continuation_eligibility_sha256": eligibility["binding_sha256"],
    })
    child["binding_sha256"] = _digest(
        {key: value for key, value in child.items() if key != "binding_sha256"}
    )
    return child


@dataclass(frozen=True)
class ContinuationPlan:
    parent_manifest_path: Path
    companion_path: Path
    eligibility_path: Path
    public_artifact_path: Path
    parent_approval_path: Path
    parent_results: Path
    child_results: Path
    stage_name: str
    parent_manifest: dict[str, Any]
    companion: dict[str, Any]
    eligibility: dict[str, Any]
    child_manifest: dict[str, Any]
    parent_rows: dict[str, dict[str, Any]]
    parent_checkpoint_sha256: str
    parent_ledger_path: Path
    parent_ledger_sha256: str
    parent_ledger_summary: dict[str, Any]
    parent_approval: BudgetApproval
    child_approval: BudgetApproval
    all_cells: tuple[dict[str, Any], ...]
    excluded_cells: tuple[dict[str, Any], ...]
    eligible_missing: tuple[dict[str, Any], ...]
    parent_manifest_file_sha256: str = ""
    companion_file_sha256: str = ""
    eligibility_file_sha256: str = ""
    public_artifact_file_sha256: str = ""
    parent_approval_file_sha256: str = ""
    audit_artifact_file_sha256: str = ""
    audit_artifact_path: Optional[Path] = None


def prepare_plan(
    *,
    parent_manifest_path: Path, companion_path: Path, eligibility_path: Path,
    public_artifact_path: Path, parent_approval_path: Path, parent_results: Path,
    child_results: Path, corpus_root: Path = DATASET_ROOT,
    dependency_lock: Optional[Path] = None, audit_artifact_path: Optional[Path] = None,
    stage_name: str = "development_pilot", live: bool = False,
) -> ContinuationPlan:
    """Validate immutable inputs and derive exactly 90 valid missing cells."""
    paths = [Path(value).resolve() for value in (
        parent_manifest_path, companion_path, eligibility_path, public_artifact_path,
        parent_approval_path, parent_results, child_results)]
    parent_manifest_path, companion_path, eligibility_path, public_artifact_path = paths[:4]
    parent_approval_path, parent_results, child_results = paths[4:]
    if stage_name != "development_pilot":
        raise ContinuationError("continuation is frozen to development_pilot")
    if child_results == parent_results or child_results.is_relative_to(parent_results):
        raise ContinuationError("child results must be a sibling tree")
    parent, companion, eligibility = (
        _read(parent_manifest_path), _read(companion_path), _read(eligibility_path)
    )
    try:
        campaign.validate_manifest(parent, companion, corpus_root=corpus_root, live=False)
    except (OSError, ValueError) as exc:
        raise ContinuationError("frozen parent manifest validation failed") from exc
    excluded = validate_eligibility(parent, eligibility, audit_artifact_path=audit_artifact_path)
    all_cells = _cells(parent, stage_name)
    expected = {_cell_key(cell): cell for cell in all_cells}
    parent_rows, checkpoint_hash = _load_checkpoints(
        parent_results / stage_name, parent["binding_sha256"], expected,
        expected_count=PARENT_TERMINAL_CHECKPOINTS,
    )
    _validate_public(public_artifact_path, parent["binding_sha256"], expected)
    ledger_path = parent_results / "spending" / f"{stage_name}.jsonl"
    parent_approval = _validate_parent_approval(
        _read(parent_approval_path), parent, stage_name, ledger_path
    )
    try:
        ledger = CampaignLedger(ledger_path, _binding(parent, stage_name), parent_approval)
        ledger_summary = ledger.summary()
    except ValueError as exc:
        raise ContinuationError("parent ledger validation failed") from exc
    if (ledger_summary.get("calls") != PARENT_TERMINAL_CALLS
            or ledger_summary.get("by_status") != {"completed": PARENT_COMPLETED_CALLS, "uncertain": PARENT_UNCERTAIN_CALLS}):
        raise ContinuationError("parent ledger is not the frozen 81-complete/1-uncertain boundary")
    eligible, excluded_missing = eligible_missing_cells(
        parent, stage_name, set(parent_rows), {_cell_key(cell) for cell in excluded}
    )
    if len(eligible) != 90 or len(excluded_missing) != 1:
        raise ContinuationError("parent boundary does not derive 90 valid and one excluded missing cell")
    parent_ledger_hash = sha256_file(ledger_path)
    parent_input_hashes = {
        "manifest": sha256_file(parent_manifest_path),
        "companion": sha256_file(companion_path),
        "eligibility": sha256_file(eligibility_path),
        "public_artifact": sha256_file(public_artifact_path),
        "approval": sha256_file(parent_approval_path),
        "ledger": parent_ledger_hash,
    }
    if audit_artifact_path is not None:
        parent_input_hashes["audit_artifact"] = sha256_file(audit_artifact_path)
    child = _child_manifest(
        parent, eligibility, parent_manifest_sha256=parent_input_hashes["manifest"],
        parent_checkpoint_sha256=checkpoint_hash,
        parent_ledger_sha256=parent_ledger_hash,
    )
    child["continuation_parent_inputs"] = parent_input_hashes
    child["binding_sha256"] = _digest(
        {key: value for key, value in child.items() if key != "binding_sha256"}
    )
    try:
        campaign.validate_manifest(
            child, companion, corpus_root=corpus_root, dependency_lock=dependency_lock, live=live
        )
    except (OSError, ValueError) as exc:
        raise ContinuationError("continuation manifest validation failed") from exc
    child_approval = derive_child_approval(
        parent_approval, parent_calls=ledger_summary["calls"],
        parent_reserved_cost_micros=ledger_summary["reserved_cost_micros"],
    )
    return ContinuationPlan(
        parent_manifest_path, companion_path, eligibility_path, public_artifact_path,
        parent_approval_path, parent_results, child_results, stage_name, parent, companion,
        eligibility, child, parent_rows, checkpoint_hash, ledger_path, sha256_file(ledger_path),
        ledger_summary, parent_approval, child_approval, tuple(all_cells), tuple(excluded),
        tuple(eligible),
        parent_manifest_file_sha256=parent_input_hashes["manifest"],
        companion_file_sha256=parent_input_hashes["companion"],
        eligibility_file_sha256=parent_input_hashes["eligibility"],
        public_artifact_file_sha256=parent_input_hashes["public_artifact"],
        parent_approval_file_sha256=parent_input_hashes["approval"],
        audit_artifact_file_sha256=parent_input_hashes.get("audit_artifact", ""),
        audit_artifact_path=audit_artifact_path,
    )


def build_continuation_client(
    plan: ContinuationPlan, *, transport_factory: Optional[Callable[..., Any]] = None,
) -> LunaResponsesClient:
    """Create the OAuth-only child client and derived child spending ledger."""
    ledger_path = plan.child_results / "spending" / f"{plan.stage_name}.jsonl"
    ledger = CampaignLedger(ledger_path, _binding(plan.child_manifest, plan.stage_name), plan.child_approval)
    oauth = plan.child_manifest.get("oauth")
    if not isinstance(oauth, dict) or oauth.get("attempt_timeout_seconds") != CONTINUATION_TIMEOUT_SECONDS:
        raise ContinuationError("child OAuth timeout is not the frozen 600-second deadline")
    if transport_factory is not None:
        transport = transport_factory(plan.child_manifest, plan.child_results, plan.child_approval)
    else:
        try:
            from eval.codex_oauth import CodexOAuthTransport
            executable = Path(str(oauth["resolved_executable"]))
            if not executable.is_absolute():
                resolved = shutil.which(str(executable))
                if resolved is None:
                    raise ContinuationError("frozen Codex executable is unavailable")
                executable = Path(resolved).resolve()
            transport = CodexOAuthTransport(
                executable=str(executable),
                expected_version=oauth["version"],
                expected_instruction_sha256=oauth["instruction_sha256"],
                expected_executable_sha256=oauth["executable_sha256"],
                work_root=(plan.child_results / "oauth-transport").resolve(),
                timeout_seconds=CONTINUATION_TIMEOUT_SECONDS,
            )
            readiness = transport.inspect()
            if any(readiness.get(key) != value for key, value in {
                "transport": "codex_oauth", "version": oauth["version"],
                "instruction_sha256": oauth["instruction_sha256"],
                "model": plan.child_manifest["model"],
                "reasoning_effort": plan.child_manifest["reasoning_effort"],
                "automatic_retries": 0,
                "billing_basis": "subscription_usage_api_price_proxy_not_invoice",
            }.items()):
                raise ContinuationError("child OAuth readiness differs from the manifest")
            if Path(str(readiness.get("executable", ""))).name != oauth.get("executable"):
                raise ContinuationError("child OAuth executable differs from the manifest")
        except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
            raise ContinuationError("child OAuth transport is unavailable") from exc
    return LunaResponsesClient(ledger, transport=transport, retries=0)


def _assert_parent_unchanged(plan: ContinuationPlan) -> None:
    checks = (
        (plan.parent_results / plan.stage_name, plan.parent_checkpoint_sha256, _checkpoint_digest),
        (plan.parent_ledger_path, plan.parent_ledger_sha256, sha256_file),
        (plan.parent_manifest_path, plan.parent_manifest_file_sha256, sha256_file),
        (plan.companion_path, plan.companion_file_sha256, sha256_file),
        (plan.eligibility_path, plan.eligibility_file_sha256, sha256_file),
        (plan.public_artifact_path, plan.public_artifact_file_sha256, sha256_file),
        (plan.parent_approval_path, plan.parent_approval_file_sha256, sha256_file),
    )
    for path, expected, reader in checks:
        if expected and reader(path) != expected:
            raise ContinuationError("immutable parent input changed during continuation")
    if plan.audit_artifact_path and plan.audit_artifact_file_sha256 and (
        sha256_file(plan.audit_artifact_path) != plan.audit_artifact_file_sha256
    ):
        raise ContinuationError("immutable corpus audit input changed during continuation")


def _claim_allocation(plan: ContinuationPlan) -> None:
    """Persist one child allocation at the parent so another directory cannot replay it."""
    receipt = {
        "schema": "engraphis-campaign-continuation-allocation/v1",
        "parent_campaign_sha256": plan.parent_manifest["binding_sha256"],
        "parent_ledger_sha256": plan.parent_ledger_sha256,
        "parent_checkpoint_sha256": plan.parent_checkpoint_sha256,
        "parent_approval_sha256": plan.parent_approval_file_sha256,
        "continuation_campaign_sha256": plan.child_manifest["binding_sha256"],
        "child_ledger_path_sha256": _digest(str(
            (plan.child_results / "spending" / f"{plan.stage_name}.jsonl").resolve())),
        "approval": plan.child_approval.public_fields(),
        "eligibility_sha256": plan.eligibility["binding_sha256"],
        "eligible_cells_sha256": _digest(list(plan.eligible_missing)),
        "authorization_basis": "continuation of unattempted cells within original approved core pilot",
    }
    path = plan.parent_results / "continuation-allocation.json"
    if path.exists():
        if _read(path) != receipt:
            raise ContinuationError("parent allowance already allocated to a different continuation")
    else:
        _save_new(path, receipt)


def _assert_child_source(plan: ContinuationPlan) -> None:
    if plan.child_manifest.get("source") != campaign.source_snapshot():
        raise ContinuationError("continuation source snapshot changed")
    if plan.child_manifest.get("continuation_source") != {
        "eval/campaign_continuation.py": sha256_file(Path(__file__).resolve())
    }:
        raise ContinuationError("continuation module changed after preflight")


@contextmanager
def _execution_lock(path: Path, label: str):
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".campaign-execution.lock"
    try:
        handle = marker.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ContinuationError(f"{label} execution lock already exists") from exc
    try:
        handle.write(f"{label}:{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.close()
        marker.unlink(missing_ok=True)


def _load_child(plan: ContinuationPlan) -> dict[str, dict[str, Any]]:
    directory = plan.child_results / plan.stage_name
    directory.mkdir(parents=True, exist_ok=True)
    expected = {_cell_key(cell): cell for cell in plan.eligible_missing}
    if list(directory.glob("*.started")):
        raise ContinuationError("unfinished child reservation requires reconciliation")
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        checkpoint = _read(path)
        cell, row = checkpoint.get("cell"), checkpoint.get("row")
        key = _cell_key(cell) if isinstance(cell, dict) else ""
        if key not in expected or key in rows:
            raise ContinuationError("child checkpoint is outside the eligible set")
        if checkpoint.get("binding_sha256") != plan.child_manifest["binding_sha256"]:
            raise ContinuationError("child checkpoint binding mismatch")
        if path.name != f"{campaign.digest(cell)}.json" or checkpoint.get("row_sha256") != _digest(row):
            raise ContinuationError("child checkpoint checksum mismatch")
        campaign.validate_row(row, cell)
        rows[key] = row
    return rows


def _lineage(row: Mapping[str, Any], plan: ContinuationPlan, *, cohort: str, eligible: bool) -> dict[str, Any]:
    value = dict(row)
    value.update({
        "cohort": cohort,
        "campaign_sha256": plan.child_manifest["binding_sha256"] if cohort == "continuation" else plan.parent_manifest["binding_sha256"],
        "source_manifest_sha256": _digest(plan.child_manifest.get("source", {}) if cohort == "continuation" else plan.parent_manifest.get("source", {})),
        "deadline_seconds": CONTINUATION_TIMEOUT_SECONDS if cohort == "continuation" else DEFAULT_TIMEOUT_SECONDS,
        "validity": "eligible" if eligible else "invalid_fixture",
        "eligible_for_quality": bool(eligible),
    })
    return value


def _error_row(cell: Mapping[str, Any], exc: BaseException, plan: ContinuationPlan) -> dict[str, Any]:
    attempt_id = "attempt-" + campaign.digest({
        "campaign": plan.child_manifest["binding_sha256"],
        "stage": plan.stage_name,
        **dict(cell),
    })[:32]
    return _lineage({
        **dict(cell), "attempt_id": attempt_id, "status": "error",
        "error_class": type(exc).__name__, "task_success": None,
        "critical_violations": [], "provider_usage": [],
    }, plan, cohort="continuation", eligible=True)


class _GuardedClient:
    def __init__(self, client: Any, check: Callable[[], None]) -> None:
        self._client, self._check = client, check

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def complete(self, **kwargs: Any) -> Any:
        self._check()
        return self._client.complete(**kwargs)


def run_continuation(
    plan: ContinuationPlan, *, corpus: Any, client: Any,
    attempt_runner: Callable[..., dict[str, Any]] = campaign.run_attempt,
    maximum_attempts: Optional[int] = None, enforce_source: bool = True,
) -> dict[str, Any]:
    """Run eligible missing cells once, preserving a marker on unsafe stops."""
    if client is None:
        raise ContinuationError("a budgeted OAuth client is required")
    if maximum_attempts is not None and (
        isinstance(maximum_attempts, bool) or not isinstance(maximum_attempts, int) or maximum_attempts < 1
    ):
        raise ContinuationError("maximum_attempts must be a positive integer")
    def before_dispatch():
        _assert_parent_unchanged(plan)
        if enforce_source:
            _assert_child_source(plan)
        if plan.parent_approval_file_sha256:
            _validate_parent_approval(_read(plan.parent_approval_path), plan.parent_manifest,
                                      plan.stage_name, plan.parent_ledger_path)
    guarded_client = _GuardedClient(client, before_dispatch)
    plan.child_results.mkdir(parents=True, exist_ok=True)
    with _execution_lock(plan.parent_results, "parent"), _execution_lock(plan.child_results, "continuation"):
        if enforce_source:
            _assert_child_source(plan)
        _assert_parent_unchanged(plan)
        _claim_allocation(plan)
        manifest_path = plan.child_results / "continuation-manifest.json"
        if manifest_path.exists():
            if _read(manifest_path) != plan.child_manifest:
                raise ContinuationError("continuation manifest changed")
        else:
            _save_new(manifest_path, plan.child_manifest)
        child_rows = _load_child(plan)
        executed = 0
        for cell in plan.eligible_missing:
            key = _cell_key(cell)
            if key in child_rows:
                if child_rows[key].get("status") == "error" or child_rows[key].get("critical_violations"):
                    break
                continue
            if maximum_attempts is not None and executed >= maximum_attempts:
                break
            _assert_parent_unchanged(plan)
            if plan.parent_approval_file_sha256:
                _validate_parent_approval(_read(plan.parent_approval_path), plan.parent_manifest,
                                          plan.stage_name, plan.parent_ledger_path)
            output = plan.child_results / plan.stage_name / f"{campaign.digest(cell)}.json"
            marker = output.with_suffix(".started")
            if marker.exists():
                raise ContinuationError("unfinished child reservation; automatic replay is forbidden")
            _save_new(marker, {
                "parent_campaign_sha256": plan.parent_manifest["binding_sha256"],
                "child_campaign_sha256": plan.child_manifest["binding_sha256"], "cell": cell,
            })
            try:
                row = attempt_runner(plan.child_manifest, plan.stage_name, dict(cell), corpus, guarded_client)
                if enforce_source:
                    _assert_child_source(plan)
                _assert_parent_unchanged(plan)
                campaign.validate_row(row, cell)
                row = _lineage(row, plan, cohort="continuation", eligible=True)
            except ContinuationError:
                raise
            except Exception as exc:
                row = _error_row(cell, exc, plan)
                campaign.validate_row(row, cell)
            _save_new(output, {
                "binding_sha256": plan.child_manifest["binding_sha256"],
                "parent_binding_sha256": plan.parent_manifest["binding_sha256"],
                "cell": cell, "row": row, "row_sha256": _digest(row),
            })
            marker.unlink(missing_ok=True)
            child_rows[key] = row
            executed += 1
            print(f"{plan.stage_name} continuation: {executed} new attempts; {row['status']}", flush=True)
            if row.get("status") == "error" or row.get("critical_violations"):
                break
        return combined_report(plan)


def _safe_record(row: Mapping[str, Any], plan: ContinuationPlan, *, cohort: str, eligible: bool, reason: Optional[str] = None) -> dict[str, Any]:
    cell = {key: row.get(key) for key in ("scenario_id", "arm", "token_budget", "repetition")}
    scenario = str(cell["scenario_id"])
    result = {
        **cell, "question_id": _question_id(cell),
        "family_id": row.get("family_id") or scenario.split(":", 1)[0],
        "category": row.get("category") or scenario.split(":", 1)[-1],
        "status": row.get("status"), "task_success": row.get("task_success"),
        "context_tokens": row.get("context_tokens"), "latency_ms": row.get("latency_ms"),
        "citation_validity": row.get("citation_validity"), "citation_support": row.get("citation_support"),
        "evidence_retention": row.get("evidence_retention"), "abstention_correct": row.get("abstention_correct"),
        "reader_calls": row.get("reader_calls"), "correction_calls": row.get("correction_calls"),
        "oracle_calls": row.get("oracle_calls"), "critical_violation_count": len(row.get("critical_violations", [])),
        "oracle_outcome": row.get("oracle_outcome"), "unscored_reason": row.get("unscored_reason"),
        "cohort": cohort,
        "campaign_sha256": plan.child_manifest["binding_sha256"] if cohort == "continuation" else plan.parent_manifest["binding_sha256"],
        "source_manifest_sha256": _digest(plan.child_manifest.get("source", {}) if cohort == "continuation" else plan.parent_manifest.get("source", {})),
        "deadline_seconds": CONTINUATION_TIMEOUT_SECONDS if cohort == "continuation" else DEFAULT_TIMEOUT_SECONDS,
        "validity": "eligible" if eligible else "invalid_fixture", "eligible_for_quality": bool(eligible),
    }
    if reason:
        result["excluded"] = {"question_id": _question_id(cell), "reason": reason}
    return result


def combined_report(plan: ContinuationPlan) -> dict[str, Any]:
    """Combine raw rows with a safe, explicit quality mask."""
    child_rows = _load_child(plan)
    raw: dict[str, tuple[dict[str, Any], str]] = {
        key: (row, "original") for key, row in plan.parent_rows.items()
    }
    for key, row in child_rows.items():
        if key in raw:
            raise ContinuationError("parent and child share a cell")
        raw[key] = (row, "continuation")
    excluded = {_cell_key(cell) for cell in plan.excluded_cells}
    records: list[dict[str, Any]] = []
    for cell in plan.all_cells:
        key = _cell_key(cell)
        if key in raw:
            row, cohort = raw[key]
            reason = (INVALID_FIXTURE_REASON if key in excluded else
                      "unscored_attempt" if row["status"] != "complete" else None)
            records.append(_safe_record(row, plan, cohort=cohort, eligible=key not in excluded, reason=reason))
    exclusion_rows = [row["excluded"] for row in records if row.get("excluded")]
    terminal = [row for row, _ in raw.values()]
    statuses = Counter(str(row.get("status")) for row in terminal)
    valid_expected = [cell for cell in plan.all_cells if _cell_key(cell) not in excluded]
    valid_observed = sum(_cell_key(cell) in raw for cell in valid_expected)
    valid_missing = len(valid_expected) - valid_observed
    raw_missing = len(plan.all_cells) - len(terminal)
    excluded_observed = sum(_cell_key(cell) in raw for cell in plan.excluded_cells)
    provider_usage = campaign._provider_usage_summary(terminal)
    eligible_rows = [row for key, (row, _) in raw.items() if key not in excluded]
    eligible_statuses = Counter(row["status"] for row in eligible_rows)
    critical = sum(len(row.get("critical_violations", [])) for row in terminal)
    child_ledger_path = plan.child_results / "spending" / f"{plan.stage_name}.jsonl"
    child_ledger_summary = (CampaignLedger(child_ledger_path, _binding(plan.child_manifest, plan.stage_name),
                           plan.child_approval).summary() if child_ledger_path.exists() else None)
    uncertain = (plan.parent_ledger_summary.get("by_status", {}).get("uncertain", 0)
                 + (child_ledger_summary or {}).get("by_status", {}).get("uncertain", 0))
    arm_names = sorted({cell["arm"] for cell in plan.all_cells})
    def arm_counts(rows):
        return {arm: {
            "attempts": sum(row["arm"] == arm for row in rows),
            "complete": sum(row["arm"] == arm and row["status"] == "complete" for row in rows),
            "successes": sum(row["arm"] == arm and row.get("task_success") is True for row in rows),
        } for arm in arm_names}
    parent_source, child_source = _digest(plan.parent_manifest.get("source", {})), _digest(plan.child_manifest.get("source", {}))
    metrics = {
        "schema": campaign.SCHEMA, "campaign_sha256": plan.parent_manifest["binding_sha256"],
        "parent_campaign_sha256": plan.parent_manifest["binding_sha256"],
        "continuation_campaign_sha256": plan.child_manifest["binding_sha256"], "stage": plan.stage_name,
        "status": "BLOCKED" if (raw_missing or valid_missing or statuses.get("error", 0) or critical or uncertain or len(excluded) != 15) else "COMPLETE",
        "expected_attempts": len(plan.all_cells), "missing_attempts": raw_missing,
        "statuses": dict(sorted(statuses.items())), "raw_terminal_attempts": len(terminal),
        "eligible_expected_attempts": len(valid_expected), "eligible_attempts": valid_observed,
        "valid_missing_attempts": valid_missing, "excluded_fixture_cells": len(excluded),
        "excluded_observed_attempts": excluded_observed, "excluded_unattempted_attempts": len(excluded) - excluded_observed,
        "uncertain_calls": uncertain,
        "critical_violations": critical,
        "arms": arm_counts(terminal), "eligible_arms": arm_counts(eligible_rows),
        "eligible_statuses": dict(eligible_statuses),
        "eligible_execution_status": ("BLOCKED" if eligible_statuses.get("error", 0) or critical else
                                      "PARTIAL" if valid_missing or eligible_statuses.get("unsupported", 0) else "COMPLETE"),
        "noninferiority": "indeterminate",
        "uncertainty": {"unit": "synthetic repository family", "families": len({
            str(row.get("family_id") or row["scenario_id"].split(":", 1)[0]) for row in eligible_rows}),
            "inferentially_usable": False, "interval": None,
            "reason": "One synthetic family; repeated budgets and arms are not independent samples."},
        "oracle_summary": campaign._oracle_summary(terminal),
        "provider_usage": provider_usage, "parent_ledger_summary": plan.parent_ledger_summary,
        "continuation_ledger_summary": child_ledger_summary,
        "parent_checkpoint_sha256": plan.parent_checkpoint_sha256, "parent_ledger_sha256": plan.parent_ledger_sha256,
        "eligibility_sha256": plan.eligibility["binding_sha256"],
        "cohorts": {
            "original": {"campaign_sha256": plan.parent_manifest["binding_sha256"], "source_manifest_sha256": parent_source, "deadline_seconds": DEFAULT_TIMEOUT_SECONDS, "terminal_rows": len(plan.parent_rows)},
            "continuation": {"campaign_sha256": plan.child_manifest["binding_sha256"], "source_manifest_sha256": child_source, "deadline_seconds": CONTINUATION_TIMEOUT_SECONDS, "terminal_rows": len(child_rows)},
            "excluded_fixture": {"campaign_sha256": plan.parent_manifest["binding_sha256"], "source_manifest_sha256": parent_source, "deadline_seconds": None, "cells": len(excluded), "observed_terminal_rows": excluded_observed},
        },
        "independent_acceptance_eligible": False, "leadership_eligible": False,
        "quality_denominator_note": "Raw status counts retain the 150-cell protocol; quality excludes all 15 long_documents cells.",
    }
    config = {
        "campaign_sha256": plan.parent_manifest["binding_sha256"], "continuation_campaign_sha256": plan.child_manifest["binding_sha256"],
        "parent_manifest_sha256": sha256_file(plan.parent_manifest_path), "parent_public_artifact_sha256": sha256_file(plan.public_artifact_path),
        "parent_checkpoint_sha256": plan.parent_checkpoint_sha256, "parent_ledger_sha256": plan.parent_ledger_sha256,
        "eligibility_sha256": plan.eligibility["binding_sha256"], "stage": plan.stage_name,
        "original_timeout_seconds": DEFAULT_TIMEOUT_SECONDS, "continuation_timeout_seconds": CONTINUATION_TIMEOUT_SECONDS,
        "raw_expected_attempts": len(plan.all_cells), "quality_expected_attempts": len(valid_expected),
        "fixture_exclusion_policy": "whole_scenario",
    }
    return report_envelope(
        suite="implementation-team coding campaign continuation", dataset_path=plan.parent_manifest_path,
        config=config, records=records, metrics=metrics, exclusions=exclusion_rows,
        git_commit=plan.child_manifest.get("repository_revision", plan.parent_manifest["repository_revision"]),
        command=("python", "-m", "eval.campaign_continuation"),
        source_paths=(Path(__file__).resolve(), plan.parent_manifest_path),
        models={"reader": {"model": plan.child_manifest["model"], "requested_model": plan.child_manifest["model"], "effective_model": plan.child_manifest["model"], "reasoning_effort": plan.child_manifest["reasoning_effort"], "transport": "codex_oauth", "provider": "engraphis_benchmark_oauth", "automatic_retries": 0}},
        token_accounting={"identity": "engraphis.codex_oauth.usage.v1", "revision": None, "scope": "reader_and_correction_calls_only", "method": "native app-server usage counters; failed calls without counters remain explicit", "transport": "codex_oauth", "billing_basis": "subscription_usage_api_price_proxy_not_invoice"},
    )


def write_combined_artifact(plan: ContinuationPlan, output: Path) -> dict[str, Any]:
    return write_canonical_artifact(combined_report(plan), output)


def _public_cli_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Expose aggregate continuation status without exporting private row fields."""
    keys = (
        "status", "stage", "expected_attempts", "missing_attempts", "raw_terminal_attempts",
        "eligible_expected_attempts", "eligible_attempts", "valid_missing_attempts",
        "excluded_fixture_cells", "excluded_observed_attempts", "uncertain_calls",
        "critical_violations", "eligible_execution_status", "independent_acceptance_eligible",
        "leadership_eligible",
    )
    return {key: metrics.get(key) for key in keys}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parent-manifest", "companion", "eligibility", "audit-artifact", "public-artifact", "parent-approval", "parent-results", "child-results"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=DATASET_ROOT)
    parser.add_argument("--dependency-lock", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--public-output", type=Path)
    parser.add_argument("--max-attempts", type=int)
    args = parser.parse_args(argv)
    try:
        plan = prepare_plan(
            parent_manifest_path=args.parent_manifest, companion_path=args.companion,
            eligibility_path=args.eligibility, public_artifact_path=args.public_artifact,
            parent_approval_path=args.parent_approval, parent_results=args.parent_results,
            child_results=args.child_results, corpus_root=args.corpus, dependency_lock=args.dependency_lock,
            audit_artifact_path=args.audit_artifact, live=args.execute,
        )
        if not args.execute:
            print(json.dumps({"schema": CONTINUATION_SCHEMA, "eligible_missing": len(plan.eligible_missing), "child_budget": plan.child_approval.public_fields()}, sort_keys=True))
            return 0
        client = build_continuation_client(plan)
        report = run_continuation(plan, corpus=load_corpus(args.corpus), client=client, maximum_attempts=args.max_attempts)
        if args.public_output:
            write_canonical_artifact(report, args.public_output)
        print(json.dumps(_public_cli_metrics(report["metrics"]), sort_keys=True))
        return 0 if report["metrics"]["valid_missing_attempts"] == 0 and not report["metrics"]["statuses"].get("error") else 2
    except (ContinuationError, OSError, ValueError) as exc:
        print(f"continuation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "CHILD_MAX_CALLS", "CHILD_MAX_COST_MICROS", "CONTINUATION_SCHEMA",
    "CONTINUATION_TIMEOUT_SECONDS", "ContinuationError", "ContinuationPlan",
    "build_continuation_client", "combined_report", "derive_child_approval",
    "eligible_missing_cells", "prepare_plan", "run_continuation",
    "validate_eligibility", "write_combined_artifact",
]


if __name__ == "__main__":
    raise SystemExit(main())
