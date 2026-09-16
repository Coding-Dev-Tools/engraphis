"""Post-run, read-only audit for a Codex OAuth coding campaign.

The auditor consumes a frozen campaign manifest, a completed public report, and
the private results directory after execution. It never starts Codex, reads
credentials, or calls a provider. Private inventory records exact file
digests and call bindings. The public result contains only aggregate counters,
safe model/usage boundaries, and hashes of the private inputs.

The transport's dispatch-time native journal digest was not retained by the
campaign ledger. Consequently this tool labels its journal digests as
post-run inventory hashes and does not claim dispatch-time sealing or
final-turn model identity.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional


MANIFEST_SCHEMA = "engraphis-benchmark-campaign/v1"
REPORT_SCHEMA = "engraphis-benchmark/v2"
LEDGER_SCHEMA = "engraphis-campaign-ledger/1"
AUDIT_SCHEMA = "engraphis-codex-oauth-audit/v1"
PRIVATE_SCHEMA = "engraphis-codex-oauth-audit-private/v1"
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "medium"
TRANSPORT = "codex_oauth"
SHA256 = re.compile(r"^[a-f0-9]{64}$")

GLOBAL_EVENTS = {
    "remoteControl/status/changed",
    "account/rateLimits/updated",
    "deprecationNotice",
    "warning",
}
THREAD_EVENTS = {"thread/started", "thread/status/changed"}
TURN_EVENTS = {
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "thread/tokenUsage/updated",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
}
NATIVE_EVENTS = GLOBAL_EVENTS | THREAD_EVENTS | TURN_EVENTS
USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
    "totalTokens": "total_tokens",
}
USAGE_TOTAL_FIELDS = tuple(USAGE_FIELDS.values())
LEDGER_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class AuditError(ValueError):
    """A safe, provider-independent audit input error."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", "surrogatepass"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _add_issue(issues: list[str], code: str) -> None:
    if code not in issues:
        issues.append(code)


def _read_json(path: Path, issues: list[str], code: str) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _add_issue(issues, code)
        return None
    if not isinstance(value, dict):
        _add_issue(issues, code)
        return None
    return value


def _read_jsonl(path: Path, issues: list[str]) -> Optional[list[dict]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _add_issue(issues, "jsonl_unreadable")
        return None
    rows: list[dict] = []
    for line in lines:
        if not line.strip():
            _add_issue(issues, "jsonl_blank_line")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            _add_issue(issues, "jsonl_malformed")
            continue
        if not isinstance(value, dict):
            _add_issue(issues, "jsonl_non_object")
            continue
        rows.append(value)
    return rows


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _write_immutable(path: Path, value: dict) -> str:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise AuditError("immutable_output_changed")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(payload)
    checksum = sha256_bytes(payload)
    sidecar = path.with_name(path.name + ".sha256")
    checksum_bytes = f"{checksum}  {path.name}\n".encode("ascii")
    if sidecar.exists():
        if sidecar.read_bytes() != checksum_bytes:
            raise AuditError("immutable_checksum_changed")
    else:
        with sidecar.open("xb") as handle:
            handle.write(checksum_bytes)
    return checksum


def _sidecar_matches(path: Path, digest: str) -> bool:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists():
        return True
    try:
        fields = sidecar.read_text(encoding="ascii").split()
    except (OSError, UnicodeError):
        return False
    return bool(fields) and fields[0] == digest


def _manifest(path: Path, issues: list[str]) -> tuple[dict, dict]:
    value = _read_json(path, issues, "manifest_unreadable")
    if value is None:
        return {}, {}
    if value.get("schema") != MANIFEST_SCHEMA:
        _add_issue(issues, "manifest_schema")
    supplied = value.get("binding_sha256")
    unsigned = {key: item for key, item in value.items() if key != "binding_sha256"}
    if not _is_sha(supplied) or sha256_json(unsigned) != supplied:
        _add_issue(issues, "manifest_binding")
    oauth = value.get("oauth")
    if not isinstance(oauth, dict):
        _add_issue(issues, "oauth_manifest_missing")
        oauth = {}
    corpus = value.get("corpus")
    expected = {
        "binding_sha256": supplied,
        "campaign_id": value.get("campaign_id"),
        "model": value.get("model"),
        "reasoning_effort": value.get("reasoning_effort"),
        "transport": value.get("transport"),
        "instruction_sha256": oauth.get("instruction_sha256"),
        "repository_revision": value.get("repository_revision"),
        "dataset_sha256": corpus.get("manifest_sha256") if isinstance(corpus, dict) else None,
        "pins_sha256": value.get("pins_sha256"),
    }
    if expected["model"] != MODEL or expected["reasoning_effort"] != REASONING_EFFORT:
        _add_issue(issues, "manifest_model")
    if expected["transport"] != TRANSPORT:
        _add_issue(issues, "manifest_transport")
    if not _is_sha(expected["instruction_sha256"]):
        _add_issue(issues, "manifest_instruction_hash")
    return value, expected


def _report(path: Path, manifest: dict, issues: list[str]) -> dict:
    value = _read_json(path, issues, "report_unreadable")
    if value is None:
        return {}
    if value.get("schema") != REPORT_SCHEMA:
        _add_issue(issues, "report_schema")
    for field in ("suite", "system", "environment", "protocol", "metrics"):
        if not isinstance(value.get(field), dict):
            _add_issue(issues, "report_envelope")
    if not isinstance(value.get("records"), list) or not isinstance(value.get("exclusions"), list):
        _add_issue(issues, "report_records")
    protocol = value.get("protocol")
    if isinstance(protocol, dict):
        config = protocol.get("config")
        if not isinstance(config, dict):
            _add_issue(issues, "report_config")
        elif value.get("system", {}).get("config_sha256") != sha256_json(config):
            _add_issue(issues, "report_config_hash")
        if protocol.get("n_total") != len(value.get("records", [])):
            _add_issue(issues, "report_denominator")
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
    config = protocol.get("config") if isinstance(protocol, dict) else {}
    binding = manifest.get("binding_sha256")
    if metrics.get("campaign_sha256") not in (None, binding):
        _add_issue(issues, "report_campaign_binding")
    if config.get("campaign_sha256") not in (None, binding):
        _add_issue(issues, "report_campaign_binding")
    try:
        from eval.benchmark import validate_report
        if validate_report(value, canonical=False):
            _add_issue(issues, "report_envelope")
    except Exception:
        _add_issue(issues, "report_validator_unavailable")
    if not _sidecar_matches(path, sha256_file(path)):
        _add_issue(issues, "report_sidecar")
    models = value.get("models")
    reader = models.get("reader") if isinstance(models, dict) else None
    oauth = manifest.get("oauth") if isinstance(manifest.get("oauth"), dict) else {}
    if not isinstance(reader, dict):
        _add_issue(issues, "report_model_metadata")
    else:
        if reader.get("effective_model") != MODEL or reader.get("reasoning_effort") != REASONING_EFFORT:
            _add_issue(issues, "report_model_metadata")
        if reader.get("transport") != TRANSPORT:
            _add_issue(issues, "report_model_metadata")
        if reader.get("instruction_sha256") not in (None, oauth.get("instruction_sha256")):
            _add_issue(issues, "report_instruction_hash")
    return value


def _discover(root: Path) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    checkpoints: list[Path] = []
    ledgers: list[Path] = []
    journals = sorted(root.rglob("events.jsonl")) if root.exists() else []
    pending = sorted(root.rglob("*.started")) if root.exists() else []
    if not root.exists():
        return checkpoints, ledgers, journals, pending
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (isinstance(value, dict) and isinstance(value.get("row"), dict)
                and isinstance(value.get("cell"), dict)
                and "binding_sha256" in value and "row_sha256" in value):
            checkpoints.append(path)
    for path in sorted(root.rglob("*.jsonl")):
        if path.name == "events.jsonl":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0]) if lines else None
        except (OSError, UnicodeError, IndexError, json.JSONDecodeError):
            continue
        if isinstance(first, dict) and first.get("kind") == "header" and "binding" in first:
            ledgers.append(path)
    return checkpoints, ledgers, journals, pending


def _question_id(cell: dict) -> Optional[str]:
    fields = ("scenario_id", "arm", "token_budget", "repetition")
    if any(field not in cell for field in fields):
        return None
    return f"{cell['scenario_id']}:{cell['arm']}:{cell['token_budget']}:{cell['repetition']}"


def _expected_question_ids(manifest: dict, stage_name: str) -> set[str]:
    stage = (manifest.get("stages") or {}).get(stage_name)
    if not isinstance(stage, dict):
        return set()
    return {
        f"{scenario}:{arm}:{budget}:{repetition}"
        for scenario in stage.get("scenario_ids", [])
        for arm in stage.get("arms", [])
        for budget in stage.get("token_budgets", [])
        for repetition in range(stage.get("repetitions", 0))
    }


def _token_usage(value: Any, issues: list[str], *, native: bool = False) -> Optional[dict]:
    if native:
        if not isinstance(value, dict) or any(field not in value for field in USAGE_FIELDS):
            _add_issue(issues, "native_usage_shape")
            return None
        value = {target: value.get(source) for source, target in USAGE_FIELDS.items()}
    if not isinstance(value, dict):
        _add_issue(issues, "usage_shape")
        return None
    result: dict[str, int] = {}
    for field in USAGE_TOTAL_FIELDS:
        item = value.get(field)
        if not _is_nonnegative_int(item):
            _add_issue(issues, "usage_integer")
            return None
        result[field] = item
    if (result["cached_input_tokens"] > result["input_tokens"]
            or result["reasoning_output_tokens"] > result["output_tokens"]
            or result["total_tokens"] < result["input_tokens"] + result["output_tokens"]):
        _add_issue(issues, "usage_invariant")
    return result


def _parse_checkpoints(
    paths: Iterable[Path],
    root: Path,
    manifest: dict,
    issues: list[str],
) -> tuple[list[dict], dict[str, dict], dict]:
    rows: list[dict] = []
    by_question: dict[str, dict] = {}
    by_attempt: dict[str, dict] = {}
    inventory: list[dict] = []
    for path in paths:
        value = _read_json(path, issues, "checkpoint_unreadable")
        if value is None:
            continue
        row = value.get("row")
        cell = value.get("cell")
        if value.get("binding_sha256") != manifest.get("binding_sha256"):
            _add_issue(issues, "checkpoint_binding")
        if not isinstance(row, dict) or not isinstance(cell, dict):
            _add_issue(issues, "checkpoint_shape")
            continue
        if value.get("row_sha256") != sha256_json(row):
            _add_issue(issues, "checkpoint_checksum")
        for field in ("scenario_id", "arm", "token_budget", "repetition"):
            if row.get(field) != cell.get(field):
                _add_issue(issues, "checkpoint_cell")
        question_id = _question_id(cell)
        if question_id is None:
            _add_issue(issues, "checkpoint_cell")
            continue
        if question_id in by_question:
            _add_issue(issues, "duplicate_checkpoint")
        row_copy = {"path": path, "value": value, "row": row, "cell": cell,
                    "question_id": question_id}
        by_question[question_id] = row_copy
        rows.append(row_copy)
        attempt_id = row.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            if row.get("status") == "complete":
                _add_issue(issues, "complete_attempt_id")
        elif attempt_id in by_attempt:
            _add_issue(issues, "duplicate_attempt")
        else:
            # Error rows may retain metered reader calls even though the
            # producer cannot safely claim a scored task result.
            by_attempt[attempt_id] = row_copy
        provider_usage = row.get("provider_usage")
        responses = row.get("private_responses")
        oracles = row.get("private_oracles")
        has_private_calls = any(
            field in row for field in ("provider_usage", "private_responses", "private_oracles",
                                       "reader_calls", "oracle_calls")
        )
        if has_private_calls:
            if not isinstance(provider_usage, list) or not isinstance(responses, list):
                _add_issue(issues, "checkpoint_private_calls")
            else:
                reader_calls = row.get("reader_calls")
                if (not _is_nonnegative_int(reader_calls)
                        or len(provider_usage) != reader_calls
                        or len(responses) != len(provider_usage)):
                    _add_issue(issues, "checkpoint_call_count")
            if not isinstance(oracles, list):
                _add_issue(issues, "checkpoint_oracles")
            else:
                oracle_calls = row.get("oracle_calls")
                if (not _is_nonnegative_int(oracle_calls) or len(oracles) != oracle_calls):
                    _add_issue(issues, "checkpoint_oracle_count")
        inventory.append({
            "relative_path": _relative(path, root),
            "sha256": sha256_file(path),
            "row_sha256": value.get("row_sha256"),
            "question_id_sha256": sha256_text(question_id),
            "status": row.get("status"),
        })
    return rows, by_attempt, {"files": inventory, "by_question": by_question}


def _parse_oracles(rows: Iterable[dict], issues: list[str]) -> dict:
    calls = 0
    timed_out = 0
    nonzero = 0
    for item in rows:
        oracle_rows = item["row"].get("private_oracles")
        if not isinstance(oracle_rows, list):
            continue
        for oracle in oracle_rows:
            calls += 1
            if not isinstance(oracle, dict):
                _add_issue(issues, "oracle_shape")
                continue
            if not isinstance(oracle.get("timed_out"), bool):
                _add_issue(issues, "oracle_shape")
            elif oracle["timed_out"]:
                timed_out += 1
            code = oracle.get("returncode")
            if isinstance(code, int) and not isinstance(code, bool) and code != 0:
                nonzero += 1
            elif code is not None and not _is_nonnegative_int(code):
                _add_issue(issues, "oracle_shape")
    if timed_out:
        _add_issue(issues, "oracle_timeout")
    if nonzero:
        _add_issue(issues, "oracle_nonzero_exit")
    return {"calls": calls, "timed_out": timed_out, "nonzero_exit": nonzero}


def _parse_ledgers(
    paths: Iterable[Path],
    root: Path,
    manifest: dict,
    issues: list[str],
) -> tuple[dict[str, dict], dict]:
    states: dict[str, dict] = {}
    inventory: list[dict] = []
    path_list = list(paths)
    for path in path_list:
        rows = _read_jsonl(path, issues)
        if rows is None:
            continue
        if not rows or rows[0].get("kind") != "header" or rows[0].get("schema_version") != LEDGER_SCHEMA:
            _add_issue(issues, "ledger_header")
            continue
        corpus = manifest.get("corpus")
        expected_binding = {
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "dataset_sha256": corpus.get("manifest_sha256") if isinstance(corpus, dict) else None,
            "config_sha256": manifest.get("binding_sha256"),
            "repo_revision": manifest.get("repository_revision"),
            "pins_sha256": manifest.get("pins_sha256"),
        }
        supplied = rows[0].get("binding")
        if not isinstance(supplied, dict) or any(
            supplied.get(key) != value for key, value in expected_binding.items()
        ):
            _add_issue(issues, "ledger_binding")
        local_ids: set[str] = set()
        for event in rows[1:]:
            kind = event.get("kind")
            if kind == "header":
                _add_issue(issues, "ledger_duplicate_header")
                continue
            call_id = event.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                _add_issue(issues, "ledger_call_id")
                continue
            if kind == "reserved":
                if call_id in states or call_id in local_ids:
                    _add_issue(issues, "duplicate_call")
                local_ids.add(call_id)
                request = event.get("request_sha256")
                if not _is_sha(request):
                    _add_issue(issues, "ledger_request_hash")
                state = {
                    "call_id": call_id,
                    "call_kind": event.get("call_kind"),
                    "status": "reserved",
                    "request_sha256": request,
                    "usage": None,
                    "response_sha256": None,
                    "response_canonical": None,
                }
                states.setdefault(call_id, state)
                continue
            state = states.get(call_id)
            if state is None:
                _add_issue(issues, "ledger_transition")
                continue
            if kind == "dispatched":
                if state["status"] != "reserved":
                    _add_issue(issues, "ledger_transition")
                state["status"] = "dispatched"
            elif kind in {"completed", "failed", "uncertain"}:
                if state["status"] not in {"reserved", "dispatched"}:
                    _add_issue(issues, "duplicate_terminal")
                    continue
                if kind == "completed":
                    usage = _token_usage(event.get("usage"), issues)
                    response_sha = event.get("response_sha256")
                    response = event.get("response")
                    if not _is_sha(response_sha) or not isinstance(response, str):
                        _add_issue(issues, "ledger_response")
                    elif response_sha != sha256_text(response):
                        _add_issue(issues, "ledger_response_hash")
                    if isinstance(response, str):
                        try:
                            state["response_canonical"] = canonical_json(json.loads(response))
                        except (TypeError, json.JSONDecodeError):
                            _add_issue(issues, "ledger_response_json")
                    state["usage"] = usage
                    state["response_sha256"] = response_sha
                state["status"] = kind
            else:
                _add_issue(issues, "ledger_event")
        inventory.append({
            "relative_path": _relative(path, root),
            "sha256": sha256_file(path),
            "call_count": len(local_ids),
            "completed_count": sum(
                1 for item in states.values()
                if item["status"] == "completed" and item["call_id"] in local_ids
            ),
            "pending_count": sum(
                1 for item in states.values()
                if item["status"] in {"reserved", "dispatched"} and item["call_id"] in local_ids
            ),
        })
    if not path_list:
        _add_issue(issues, "ledger_missing")
    return states, {"files": inventory}


def _parse_journals(
    paths: Iterable[Path],
    root: Path,
    manifest_expected: dict,
    issues: list[str],
) -> tuple[dict[str, dict], dict]:
    journals: dict[str, dict] = {}
    inventory: list[dict] = []
    path_list = list(paths)
    for path in path_list:
        rows = _read_jsonl(path, issues)
        if rows is None:
            continue
        entry = {
            "relative_path": _relative(path, root),
            "sha256": sha256_file(path),
            "event_count": len(rows),
            "call_id": None,
            "request_sha256": None,
            "turn_binding_count": sum(item.get("event") == "turn_binding" for item in rows),
            "usage_event_count": 0,
            "native_turn_complete": False,
            "output_sha256": None,
            "usage": None,
        }
        if len(rows) < 2 or rows[0].get("event") != "dispatch" or rows[1].get("event") != "turn_binding":
            _add_issue(issues, "journal_binding")
            inventory.append(entry)
            continue
        dispatch = rows[0]
        binding = rows[1]
        call_id = dispatch.get("call_id")
        request_sha = dispatch.get("request_sha256")
        thread_id = dispatch.get("thread_id")
        turn_id = binding.get("turn_id")
        entry.update({"call_id": call_id, "request_sha256": request_sha})
        if entry["turn_binding_count"] != 1:
            _add_issue(issues, "journal_turn_binding")
        if not isinstance(call_id, str) or not call_id:
            _add_issue(issues, "journal_call_id")
        if not _is_sha(request_sha):
            _add_issue(issues, "journal_request_hash")
        if binding.get("call_id") != call_id or binding.get("request_sha256") != request_sha:
            _add_issue(issues, "journal_binding")
        if binding.get("thread_id") != thread_id or not isinstance(thread_id, str):
            _add_issue(issues, "journal_binding")
        if not isinstance(turn_id, str) or not turn_id:
            _add_issue(issues, "journal_binding")
        if dispatch.get("model") != MODEL or dispatch.get("effort") != REASONING_EFFORT:
            _add_issue(issues, "journal_model")
        if dispatch.get("instruction_sha256") != manifest_expected.get("instruction_sha256"):
            _add_issue(issues, "journal_instruction_hash")
        if isinstance(call_id, str) and call_id in journals:
            _add_issue(issues, "duplicate_journal_call")
        output_parts: list[str] = []
        native_usage: Optional[dict] = None
        completed_count = 0
        usage_count = 0
        for item in rows[2:]:
            method = item.get("event")
            if method in NATIVE_EVENTS:
                if item.get("thread_id") != thread_id or item.get("turn_id") != turn_id:
                    _add_issue(issues, "journal_event_binding")
            elif method == "turn_binding":
                _add_issue(issues, "journal_turn_binding")
            elif method == "output":
                text = item.get("text")
                if not isinstance(text, str):
                    _add_issue(issues, "journal_output")
                else:
                    output_parts.append(text)
            elif method == "usage":
                usage_count += 1
                if usage_count > 1:
                    _add_issue(issues, "journal_usage_duplicate")
                native_usage = _token_usage(item.get("usage"), issues, native=True)
            else:
                _add_issue(issues, "journal_event")
            if isinstance(method, str) and "rerout" in method.casefold():
                _add_issue(issues, "journal_forbidden_event")
            if method in {"error", "turn/retry", "turn/error", "model/changed"}:
                _add_issue(issues, "journal_forbidden_event")
            if method == "turn/completed":
                completed_count += 1
        if rows[-1].get("event") != "turn/completed" or completed_count != 1:
            _add_issue(issues, "native_turn_incomplete")
        if not output_parts:
            _add_issue(issues, "journal_output")
        if native_usage is None:
            _add_issue(issues, "journal_usage")
        entry["native_turn_complete"] = rows[-1].get("event") == "turn/completed" and completed_count == 1
        entry["output_sha256"] = sha256_text("\n".join(output_parts)) if output_parts else None
        entry["usage"] = native_usage
        entry["usage_event_count"] = usage_count
        if isinstance(call_id, str):
            journals[call_id] = entry
        inventory.append(entry.copy())
    if not path_list:
        _add_issue(issues, "journal_missing")
    return journals, {"files": inventory}


def _call_parts(call_id: str) -> tuple[Optional[str], Optional[int]]:
    match = re.fullmatch(r"(.+)-reader-([0-9]+)", call_id)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def _join_evidence(
    rows_by_attempt: dict[str, dict],
    ledger: dict[str, dict],
    journals: dict[str, dict],
    issues: list[str],
) -> dict:
    completed = {
        call_id: state for call_id, state in ledger.items()
        if state.get("status") == "completed"
    }
    calls_by_attempt: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    for call_id, state in ledger.items():
        attempt_id, index = _call_parts(call_id)
        if attempt_id is None or index is None:
            _add_issue(issues, "call_id_shape")
            continue
        row_copy = rows_by_attempt.get(attempt_id)
        if row_copy is None:
            _add_issue(issues, "call_checkpoint_binding")
        else:
            row_usage = row_copy["row"].get("provider_usage")
            if isinstance(row_usage, list):
                calls_by_attempt[attempt_id].append((index, call_id))
                if index >= len(row_usage):
                    _add_issue(issues, "checkpoint_usage_binding")
                elif state.get("status") == "completed":
                    if _token_usage(row_usage[index], issues) != state.get("usage"):
                        _add_issue(issues, "checkpoint_usage_binding")
        journal = journals.get(call_id)
        if journal is None:
            if state.get("status") in {"completed", "dispatched", "uncertain", "failed"}:
                _add_issue(issues, "call_journal_binding")
            continue
        # Every observed native journal must bind back to the ledger request,
        # including interrupted and failed calls that have no usage yet.
        if journal.get("request_sha256") != state.get("request_sha256"):
            _add_issue(issues, "request_binding")
        status = state.get("status")
        if status == "completed":
            if not journal.get("native_turn_complete"):
                _add_issue(issues, "native_turn_incomplete")
            if state.get("response_sha256") != journal.get("output_sha256"):
                _add_issue(issues, "output_binding")
            ledger_usage = state.get("usage")
            native_usage = journal.get("usage")
            if ledger_usage is None or native_usage is None or ledger_usage != native_usage:
                _add_issue(issues, "usage_binding")
            if row_copy is None:
                _add_issue(issues, "checkpoint_response_binding")
            else:
                responses = row_copy["row"].get("private_responses")
                response_canonical = state.get("response_canonical")
                if (
                    not isinstance(responses, list)
                    or index >= len(responses)
                    or response_canonical is None
                    or canonical_json(responses[index]) != response_canonical
                ):
                    _add_issue(issues, "checkpoint_response_binding")
        elif journal.get("native_turn_complete"):
            # A terminal native turn paired with a non-terminal ledger call is
            # evidence of an accounting gap and must remain blocked.
            _add_issue(issues, "noncomplete_call_with_complete_turn")
    for call_id in journals:
        if call_id not in ledger:
            _add_issue(issues, "journal_without_ledger")
    for call_id, state in ledger.items():
        if state.get("status") in {"reserved", "dispatched"}:
            _add_issue(issues, "pending_call")
        if state.get("status") in {"failed", "uncertain"}:
            _add_issue(issues, "noncomplete_call")
    for attempt_id, row_copy in rows_by_attempt.items():
        row = row_copy["row"]
        usage = row.get("provider_usage")
        calls = sorted(calls_by_attempt.get(attempt_id, []))
        if not isinstance(usage, list):
            continue
        expected = len(usage)
        if [index for index, _ in calls] != list(range(expected)):
            _add_issue(issues, "checkpoint_call_binding")
        reader_calls = row.get("reader_calls")
        if _is_nonnegative_int(reader_calls) and reader_calls != expected:
            _add_issue(issues, "checkpoint_call_count")
    incomplete_journals = sum(not bool(item.get("native_turn_complete")) for item in journals.values())
    missing_usage = sum(
        state.get("status") in {"completed", "failed", "uncertain"} and state.get("usage") is None
        for state in ledger.values()
    )
    missing_output = sum(
        state.get("status") == "completed" and (
            state.get("response_sha256") is None or journals.get(call_id, {}).get("output_sha256") is None
        )
        for call_id, state in ledger.items()
    )
    return {
        "completed_calls": len(completed),
        "journal_calls": len(journals),
        "native_turns_complete": sum(bool(item.get("native_turn_complete")) for item in journals.values()),
        "native_turns_incomplete": incomplete_journals,
        "calls_without_usage": missing_usage,
        "completed_calls_without_output": missing_output,
        "uncertain_calls": sum(state.get("status") == "uncertain" for state in ledger.values()),
        "failed_calls": sum(state.get("status") == "failed" for state in ledger.values()),
    }


def _report_bindings(
    report: dict,
    checkpoint_data: dict,
    manifest: dict,
    stage_name: Optional[str],
    issues: list[str],
) -> None:
    records = report.get("records") if isinstance(report.get("records"), list) else []
    public_by_id: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("question_id"), str):
            continue
        if record["question_id"] in public_by_id:
            _add_issue(issues, "duplicate_report_record")
        public_by_id[record["question_id"]] = record
    private_by_id = checkpoint_data["by_question"]
    expected = _expected_question_ids(manifest, stage_name) if stage_name else set()
    if expected and set(private_by_id) != expected:
        _add_issue(issues, "checkpoint_denominator")
    if set(public_by_id) != set(private_by_id):
        _add_issue(issues, "report_checkpoint_binding")
    for question_id, item in private_by_id.items():
        public = public_by_id.get(question_id)
        if public is None:
            continue
        row = item["row"]
        for field in ("status", "task_success", "reader_calls", "context_tokens", "token_budget"):
            if field in public and public.get(field) != row.get(field):
                _add_issue(issues, "report_checkpoint_binding")
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    if metrics.get("status") != "COMPLETE":
        _add_issue(issues, "report_not_complete")
    if metrics.get("missing_attempts") not in (None, 0):
        _add_issue(issues, "report_missing_attempts")
    if metrics.get("critical_violations") not in (None, 0):
        _add_issue(issues, "report_critical_violations")
    if stage_name and metrics.get("stage") not in (None, stage_name):
        _add_issue(issues, "report_stage")


def _usage_totals(ledger: dict[str, dict]) -> dict:
    totals = {field: 0 for field in LEDGER_USAGE_FIELDS}
    for state in ledger.values():
        usage = state.get("usage")
        if not isinstance(usage, dict):
            continue
        for field in LEDGER_USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
    return totals


def audit_run(
    *,
    manifest_path: Path,
    report_path: Path,
    results: Path,
    private_inventory: Path,
    output: Path,
) -> dict:
    """Audit private post-run evidence and write private/public artifacts."""
    issues: list[str] = []
    manifest, expected = _manifest(manifest_path, issues)
    report = _report(report_path, manifest, issues)
    manifest_digest = sha256_file(manifest_path) if manifest_path.exists() else None
    report_digest = sha256_file(report_path) if report_path.exists() else None
    checkpoints, ledgers, journals, pending = _discover(results)
    checkpoint_rows, rows_by_attempt, checkpoint_inventory = _parse_checkpoints(
        checkpoints, results, manifest, issues
    )
    ledger_states, ledger_inventory = _parse_ledgers(ledgers, results, manifest, issues)
    journal_states, journal_inventory = _parse_journals(journals, results, expected, issues)
    stage_names = sorted({
        _relative(item["path"], results).split("/")[0]
        for item in checkpoint_rows if item.get("path")
    })
    stage_name = stage_names[0] if len(stage_names) == 1 else None
    if len(stage_names) != 1:
        _add_issue(issues, "stage_ambiguous")
    _report_bindings(report, checkpoint_inventory, manifest, stage_name, issues)
    joins = _join_evidence(rows_by_attempt, ledger_states, journal_states, issues)
    oracle_counts = _parse_oracles(checkpoint_rows, issues)
    pending_inventory = [
        {"relative_path": _relative(path, results), "sha256": sha256_file(path)}
        for path in pending
    ]
    if pending_inventory:
        _add_issue(issues, "pending_checkpoint")
    if not checkpoint_rows:
        _add_issue(issues, "checkpoint_missing")
    private_inventory_value = {
        "schema": PRIVATE_SCHEMA,
        "auditor_source_sha256": sha256_file(Path(__file__)),
        "claim_boundary": (
            "post-run file hashes only; transport dispatch-time journal sealing was not retained; "
            "final-turn model identity was not captured"
        ),
        "manifest_sha256": manifest_digest,
        "report_sha256": report_digest,
        "binding_sha256": manifest.get("binding_sha256"),
        "stage": stage_name,
        "checkpoints": checkpoint_inventory["files"],
        "ledgers": ledger_inventory["files"],
        "journals": journal_inventory["files"],
        "pending_markers": pending_inventory,
        "calls": [
            {
                "call_id": call_id,
                "request_sha256": state.get("request_sha256"),
                "status": state.get("status"),
                "response_sha256": state.get("response_sha256"),
                "usage": state.get("usage"),
                "journal_sha256": journal_states.get(call_id, {}).get("sha256"),
            }
            for call_id, state in sorted(ledger_states.items())
        ],
        "counts": {
            "checkpoints": len(checkpoints),
            "ledgers": len(ledgers),
            "journals": len(journals),
            "pending_markers": len(pending),
            "ledger_calls": len(ledger_states),
            "completed_calls": joins["completed_calls"],
            "uncertain_calls": joins["uncertain_calls"],
            "failed_calls": joins["failed_calls"],
            "calls_without_usage": joins["calls_without_usage"],
            "completed_calls_without_output": joins["completed_calls_without_output"],
            "native_turns_complete": joins["native_turns_complete"],
            "native_turns_incomplete": joins["native_turns_incomplete"],
            "oracle_calls": oracle_counts["calls"],
            "oracle_timeouts": oracle_counts["timed_out"],
            "oracle_nonzero": oracle_counts["nonzero_exit"],
        },
        "issues": sorted(issues),
    }
    inventory_digest = _write_immutable(private_inventory, private_inventory_value)
    usage_totals = _usage_totals(ledger_states)
    public = {
        "schema": AUDIT_SCHEMA,
        "auditor_source_sha256": sha256_file(Path(__file__)),
        "status": "COMPLETE" if not issues else "BLOCKED",
        "claim_boundary": (
            "post-run inventory and ledger/event consistency only; no dispatch-time sealing, "
            "account identity, raw output, or final-turn model identity claim"
        ),
        "manifest_sha256": manifest_digest,
        "report_sha256": report_digest,
        "private_inventory_sha256": inventory_digest,
        "stage": stage_name,
        "counts": {
            "checkpoints": len(checkpoints),
            "journals": len(journals),
            "ledger_calls": len(ledger_states),
            "completed_calls": joins["completed_calls"],
            "uncertain_calls": joins["uncertain_calls"],
            "failed_calls": joins["failed_calls"],
            "calls_without_usage": joins["calls_without_usage"],
            "completed_calls_without_output": joins["completed_calls_without_output"],
            "native_turns_complete": joins["native_turns_complete"],
            "native_turns_incomplete": joins["native_turns_incomplete"],
            "pending_calls": sum(
                state.get("status") in {"reserved", "dispatched"}
                for state in ledger_states.values()
            ),
            "oracle_calls": oracle_counts["calls"],
            "oracle_timeouts": oracle_counts["timed_out"],
            "oracle_nonzero": oracle_counts["nonzero_exit"],
        },
        "models": {
            "requested_model": MODEL,
            "effective_model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "transport": TRANSPORT,
            "instruction_sha256": expected.get("instruction_sha256"),
            "verification": "native_thread_start_and_post_run_dispatch_match",
            "final_turn_model_identity": "not_captured",
        },
        "usage": {
            "scope": "reader_and_correction_calls_only",
            "transport": TRANSPORT,
            "billing_basis": "subscription_usage_api_price_proxy_not_invoice",
            "input_tokens": usage_totals["input_tokens"],
            "cached_input_tokens": usage_totals["cached_input_tokens"],
            "output_tokens": usage_totals["output_tokens"],
            "reasoning_output_tokens": usage_totals["reasoning_output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
        },
        "oracle": {
            "status": "blocked" if oracle_counts["timed_out"] or oracle_counts["nonzero_exit"] else "complete",
            "calls": oracle_counts["calls"],
            "timeout_count": oracle_counts["timed_out"],
            "nonzero_exit_count": oracle_counts["nonzero_exit"],
        },
        "privacy": {
            "call_ids": "omitted",
            "request_hashes": "omitted",
            "response_hashes": "omitted",
            "journal_paths": "omitted",
            "raw_output": "omitted",
            "account_identity": "omitted",
        },
        "issues": sorted(issues),
    }
    _write_immutable(output, public)
    return public


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--private-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = audit_run(
            manifest_path=args.manifest,
            report_path=args.report,
            results=args.results,
            private_inventory=args.private_inventory,
            output=args.output,
        )
    except (AuditError, OSError, ValueError) as exc:
        print(f"OAuth audit failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
