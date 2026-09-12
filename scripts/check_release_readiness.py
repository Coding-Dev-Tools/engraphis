"""Validate candidate-bound full-product evidence without authorizing publication.

PASS means the recorded gate has complete, consistent, hash-verified evidence.
This checker cannot establish human identity or prove that an operator observation
actually occurred. Final release authority remains outside this local checker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA = "engraphis-product-readiness/v1"
RECEIPT_SCHEMA = "engraphis-readiness-receipt/v1"
COMPONENTS = ("engine", "cloud", "team", "edge", "website")
RELEASE_GATES = (
    "automated", "memory_integrity", "installed_journeys", "capacity",
    "responsiveness", "resource_stability", "recovery", "hosted_journeys",
    "independent_quality", "usability", "pilot",
)
LEADERSHIP_GATES = ("competitive_coding", "external_benchmarks")
STATES = {"PASS", "FAIL", "UNVERIFIED"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_MAX_BYTES = 8 * 1024 * 1024


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def candidate_id(components: dict) -> str:
    return hashlib.sha256(canonical_bytes(components)).hexdigest()


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    with path.open("rb") as source:
        payload = source.read(_MAX_BYTES + 1)
    return _decode_json(payload)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _decode_json(payload: bytes) -> Any:
    if len(payload) > _MAX_BYTES:
        raise ValueError("evidence input exceeds size limit")
    return json.loads(payload.decode("utf-8-sig"),
                      object_pairs_hook=_object_pairs,
                      parse_float=_finite_float,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError("non-finite JSON number")))


def new_ledger(components: dict) -> dict:
    """Create an explicitly incomplete ledger; this does not run any gate."""
    return {
        "schema": SCHEMA, "candidate_id": candidate_id(components),
        "components": components,
        "gates": [{"id": gate, "status": "UNVERIFIED", "owner": "release-owner",
                   "depends_on": [], "evidence": [],
                   "blockers": ["Candidate-specific evidence has not been recorded."]}
                  for gate in RELEASE_GATES + LEADERSHIP_GATES],
    }


def _receipt_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("evidence path must be a relative POSIX path")
    item = Path(relative)
    if item.is_absolute() or ":" in relative or ".." in item.parts:
        raise ValueError("evidence path escapes its root")
    resolved_root = root.resolve(strict=True)
    path = root / item
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("linked evidence inputs are not accepted")
    path.resolve(strict=True).relative_to(resolved_root)
    if not path.is_file():
        raise ValueError("evidence input must be a regular file")
    return path


def _verify_reference(root: Path, reference: Any, *, json_receipt: bool = False) -> Path:
    if not isinstance(reference, dict):
        raise ValueError("reference must be an object")
    path = _receipt_path(root, reference.get("path"))
    digest = reference.get("sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("invalid evidence digest")
    if json_receipt and path.stat().st_size > _MAX_BYTES:
        raise ValueError("evidence input exceeds size limit")
    actual = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise ValueError("evidence digest mismatch")
    return path


def _verified_json(root: Path, reference: Any) -> tuple[Path, Any]:
    if not isinstance(reference, dict):
        raise ValueError("reference must be an object")
    path = _receipt_path(root, reference.get("path"))
    with path.open("rb") as source:
        payload = source.read(_MAX_BYTES + 1)
    if len(payload) > _MAX_BYTES:
        raise ValueError("evidence input exceeds size limit")
    if hashlib.sha256(payload).hexdigest() != reference.get("sha256"):
        raise ValueError("evidence digest mismatch")
    return path, _decode_json(payload)


def _execution_result(report: Any, reference: dict) -> bool:
    """Check explicit machine outcomes, never infer success from an opaque log."""
    if not isinstance(report, dict):
        raise ValueError("execution report must be a JSON object")
    failed = (report.get("passed") is False or report.get("valid") is False
              or report.get("status") in ("FAIL", "failed", "failure", "error")
              or report.get("conclusion") in ("failure", "cancelled", "timed_out"))
    if "exit_code" in report:
        if type(report["exit_code"]) is not int:
            raise ValueError("execution exit_code must be an integer")
        failed = failed or report["exit_code"] != 0
    suite = report.get("suite")
    capacity_matrix = isinstance(suite, dict) and suite.get("name") == "engraphis-capacity-matrix/v1"
    if capacity_matrix:
        # Report-only and synthetic aggregation can exit successfully. Neither a
        # structural pass nor an arbitrary true child qualifies the real matrix.
        metrics = report.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("capacity matrix is missing acceptance metrics")
        failed = failed or metrics.get("fixture") is not False or any(
            metrics.get(name) is not True for name in (
                "capacity_acceptance_pass", "responsiveness_gate_pass",
                "resource_stability_gate_pass", "all_measurements_complete",
            )
        )
        failed = failed or any(type(metrics.get(name)) is not int or metrics[name] != count
                               for name, count in (("cell_count", 48), ("repetition_count", 240),
                                                   ("scheduled_operations", 480000)))
    required = reference.get("required_true", [])
    if not isinstance(required, list) or any(
        not isinstance(pointer, str) or not pointer.startswith("/") for pointer in required
    ):
        raise ValueError("required_true must contain JSON pointers")
    for pointer in required:
        value = report
        for part in pointer[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or key not in value:
                raise ValueError("required execution boolean is absent: " + pointer)
            value = value[key]
        failed = failed or value is not True
    if failed:
        return False
    if not (report.get("passed") is True or report.get("valid") is True
            or type(report.get("exit_code")) is int and report["exit_code"] == 0
            or required or capacity_matrix):
        raise ValueError("execution report has no explicit checked outcome")
    if "release_gates" in report:
        selection = reference.get("planned_recall_gate")
        if not isinstance(selection, dict) or set(selection) != {"candidate", "level"}:
            raise ValueError("evaluation report requires an explicit planned-recall candidate and level")
        from eval.planned_recall import require_gate
        require_gate(report, selection["candidate"], level=selection["level"])
    return True


def validate(ledger: Any, evidence_root: Path, *, engine_root: Optional[Path] = None) -> dict:
    errors: list[str] = []
    blocked: list[str] = []
    if not isinstance(ledger, dict) or ledger.get("schema") != SCHEMA:
        return {"valid": False, "errors": ["unsupported readiness ledger"],
                "release_gate_status": "FAIL", "leadership_gate_status": "FAIL",
                "publication_authorized": False, "execution_authenticity_verified": False}
    components = ledger.get("components")
    if not isinstance(components, dict) or set(components) != set(COMPONENTS):
        errors.append("exactly engine, cloud, team, edge and website identities are required")
        components = {}
    artifact_complete = True
    for name, identity in components.items():
        if not isinstance(identity, dict):
            errors.append(name + ": component must be an object")
            continue
        if not _COMMIT.fullmatch(str(identity.get("commit", ""))):
            errors.append(name + ": exact source commit is required")
        if not _HASH.fullmatch(str(identity.get("artifact_sha256", ""))):
            artifact_complete = False
            blocked.append(name + ": built/deployed artifact digest is unverified")
        elif not identity.get("artifact_path"):
            artifact_complete = False
            blocked.append(name + ": component artifact bytes are unavailable")
        else:
            try:
                _verify_reference(evidence_root, {"path": identity["artifact_path"],
                                  "sha256": identity["artifact_sha256"]})
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(name + ": " + str(exc))
    try:
        bound_id = candidate_id(components)
    except (ValueError, TypeError):
        bound_id = ""
        errors.append("component identity is not canonical JSON")
    if ledger.get("candidate_id") != bound_id:
        errors.append("candidate identity does not match components")
    gates = ledger.get("gates")
    expected = set(RELEASE_GATES + LEADERSHIP_GATES)
    if not isinstance(gates, list) or any(not isinstance(gate, dict) for gate in gates):
        errors.append("gates must be an object array")
        gates = []
    gate_ids = [gate.get("id") for gate in gates]
    if any(not isinstance(name, str) for name in gate_ids):
        errors.append("gate IDs must be strings")
    elif set(gate_ids) != expected or len(gate_ids) != len(expected):
        errors.append("every required gate must occur exactly once")
    statuses = {gate.get("id"): gate.get("status") for gate in gates
                if isinstance(gate.get("id"), str)}
    for gate in gates:
        name = str(gate.get("id"))
        status = gate.get("status")
        if not isinstance(status, str) or status not in STATES:
            errors.append(name + ": invalid status")
        owner = gate.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            errors.append(name + ": accountable owner is required")
        dependencies = gate.get("depends_on")
        if (not isinstance(dependencies, list)
                or any(not isinstance(dep, str) or dep not in expected or dep == name
                       for dep in dependencies)):
            errors.append(name + ": invalid dependencies")
            dependencies = []
        if status == "PASS" and any(statuses.get(dep) != "PASS" for dep in dependencies):
            errors.append(name + ": prerequisite has not passed")
        reasons = gate.get("blockers")
        if (not isinstance(reasons, list)
                or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)):
            errors.append(name + ": blockers must be nonempty strings")
            reasons = []
        if status == "PASS" and reasons:
            errors.append(name + ": PASS cannot retain unresolved blockers")
        if status != "PASS":
            blocked.append(name)
            if not reasons:
                errors.append(name + ": missing blocker explanation")
        references = gate.get("evidence")
        if not isinstance(references, list):
            errors.append(name + ": evidence must be an array")
            references = []
        if status == "PASS" and not references:
            errors.append(name + ": PASS requires candidate-bound evidence")
        seen_paths: set[str] = set()
        for reference in references:
            try:
                path, receipt = _verified_json(evidence_root, reference)
                if str(path) in seen_paths:
                    raise ValueError("duplicate evidence reference")
                seen_paths.add(str(path))
                if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
                    raise ValueError("unsupported gate receipt")
                if receipt.get("candidate_id") != bound_id or receipt.get("gate_id") != name:
                    raise ValueError("receipt belongs to another candidate or gate")
                if type(receipt.get("passed")) is not bool:
                    raise ValueError("receipt passed must be boolean")
                if receipt["passed"] is False and status != "FAIL":
                    raise ValueError("failed receipt must be reflected by FAIL status")
                if status == "PASS" and receipt["passed"] is not True:
                    raise ValueError("failed receipt cannot support PASS")
                observed = datetime.fromisoformat(str(receipt.get("observed_at", "")).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError("observation time must include timezone")
                if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
                    raise ValueError("observation time is in the future")
                if receipt.get("evidence_kind") not in ("automated", "attended"):
                    raise ValueError("evidence kind must be automated or attended")
                if not isinstance(receipt.get("summary"), str) or not receipt["summary"].strip():
                    raise ValueError("receipt requires a concise observation summary")
                artifacts = receipt.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise ValueError("receipt requires underlying execution artifacts")
                artifact_paths = set()
                execution_count = 0
                for artifact in artifacts:
                    artifact_path = _verify_reference(evidence_root, artifact)
                    if artifact_path == path or str(artifact_path) in artifact_paths:
                        raise ValueError("duplicate or self-referencing execution artifact")
                    artifact_paths.add(str(artifact_path))
                    role = artifact.get("role", "execution")
                    if role not in ("execution", "attachment"):
                        raise ValueError("artifact role must be execution or attachment")
                    if role == "execution":
                        execution_count += 1
                        _, report = _verified_json(evidence_root, artifact)
                        successful = _execution_result(report, artifact)
                        if not successful and receipt["passed"] is True:
                            raise ValueError("failed execution artifact cannot support a passing receipt")
                    elif artifact_path.suffix.lower() == ".json":
                        _, report = _verified_json(evidence_root, artifact)
                        if isinstance(report, dict) and (any(key in report for key in (
                            "passed", "valid", "exit_code", "status", "conclusion", "release_gates"
                        )) or isinstance(report.get("suite"), dict)
                            and report["suite"].get("name") == "engraphis-capacity-matrix/v1"):
                            if not _execution_result(report, artifact) and receipt["passed"] is True:
                                raise ValueError("failed attachment cannot support a passing receipt")
                if receipt["evidence_kind"] == "automated" and not execution_count:
                    raise ValueError("automated receipt requires a checked execution report")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(name + ": " + str(exc))
    # Cycles cannot make mutually dependent PASS assertions self-supporting.
    edges = {gate["id"]: gate.get("depends_on", []) for gate in gates
             if isinstance(gate.get("id"), str) and isinstance(gate.get("depends_on"), list)}
    def visit(name: str, active: set[str], complete: set[str]) -> bool:
        if name in active:
            return False
        if name in complete:
            return True
        active.add(name)
        for dependency in edges.get(name, []):
            if isinstance(dependency, str) and not visit(dependency, active, complete):
                return False
        active.remove(name)
        complete.add(name)
        return True
    completed: set[str] = set()
    if any(not visit(name, set(), completed) for name in edges):
        errors.append("gate dependency cycle")
    engine_verified = False
    if engine_root is not None:
        try:
            actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=engine_root,
                                             text=True, stderr=subprocess.DEVNULL).strip()
            dirty = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                                            cwd=engine_root, text=True, stderr=subprocess.DEVNULL).strip()
            indexed = subprocess.check_output(["git", "ls-files", "-v", "-z"],
                                              cwd=engine_root, stderr=subprocess.DEVNULL)
            hidden = any(entry[:1].islower() or entry[:1] == b"S"
                         for entry in indexed.split(b"\0") if entry)
            engine_identity = components.get("engine")
            if (not isinstance(engine_identity, dict)
                    or actual != engine_identity.get("commit") or dirty or hidden):
                errors.append("engine checkout must be clean and match the candidate commit")
            else:
                engine_verified = True
        except (OSError, subprocess.CalledProcessError):
            errors.append("engine checkout identity could not be verified")
    release_complete = (not errors and artifact_complete
                        and all(statuses.get(name) == "PASS" for name in RELEASE_GATES))
    leadership_complete = release_complete and all(
        statuses.get(name) == "PASS" for name in LEADERSHIP_GATES)
    release_failed = bool(errors) or any(statuses.get(name) == "FAIL" for name in RELEASE_GATES)
    leadership_failed = release_failed or any(statuses.get(name) == "FAIL" for name in LEADERSHIP_GATES)
    return {
        "schema": "engraphis-readiness-check/v1", "candidate_id": bound_id,
        "valid": not errors, "errors": errors, "blocked": blocked,
        "release_gate_status": "PASS" if release_complete else "FAIL" if release_failed else "UNVERIFIED",
        "leadership_gate_status": "PASS" if leadership_complete else "FAIL" if leadership_failed else "UNVERIFIED",
        "engine_checkout_verified": engine_verified,
        "execution_authenticity_verified": False, "publication_authorized": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--require-release", action="store_true")
    parser.add_argument("--require-leadership", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate(read_json(args.ledger), args.evidence_root, engine_root=args.engine_root)
    except (OSError, ValueError, TypeError) as exc:
        result = {"valid": False, "errors": [str(exc)]}
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    if not result["valid"]:
        return 1
    if args.require_release or args.require_leadership:
        if not result.get("engine_checkout_verified") or result.get("release_gate_status") != "PASS":
            return 1
    if args.require_leadership and result.get("leadership_gate_status") != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
