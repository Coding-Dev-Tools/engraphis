"""Durable, hash-bound budget ledger for benchmark campaign calls.

The campaign runner may be interrupted between reserving a provider call and
receiving its response.  This module treats that interval as uncertain and
never silently dispatches the same call again.  Ledger records contain hashes,
bounded labels, counters, and a normalized response only; prompts, contexts,
credentials, and provider error text are deliberately excluded.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:  # pragma: no cover - the Windows branch is exercised on release hosts.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = "engraphis-campaign-ledger/1"
CALL_KINDS = frozenset({"ingest", "reader", "evaluator", "correction"})
# The frozen campaign contract permits 4,096 output tokens.  A 4 MiB UTF-8
# journal bound leaves 1 KiB per output token, covering code-heavy and
# non-ASCII responses while retaining a bounded append-only record.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


class CampaignLedgerError(ValueError):
    """A safe, non-provider-specific campaign ledger error."""


class BudgetExceeded(CampaignLedgerError):
    """A reservation would exceed the approved call or cash ceiling."""


class UncertainCall(CampaignLedgerError):
    """A previous dispatch has no durable completion and cannot be retried."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()


def _digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignLedgerError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _label(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise CampaignLedgerError(f"{field} must be a bounded lowercase label")
    return value


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignLedgerError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    result = _nonnegative_int(value, field=field)
    if result <= 0:
        raise CampaignLedgerError(f"{field} must be positive")
    return result


def _normalized_response(value: Any) -> str:
    if not isinstance(value, str):
        raise CampaignLedgerError("response must be a string")
    # Preserve the provider's exact text so a resumed JSON answer or citation
    # payload is byte-identical.  NUL is the one control byte disallowed by the
    # line-oriented journal; all other bounded text (including whitespace and
    # newlines) remains intact.
    if "\x00" in value:
        raise CampaignLedgerError("response contains an unsafe NUL byte")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_RESPONSE_BYTES:
        raise CampaignLedgerError("response exceeds the campaign ledger size cap")
    return value


@dataclass(frozen=True)
class CampaignBinding:
    """Immutable identity shared by all records in one campaign."""

    campaign_id: str
    model: str
    reasoning_effort: str
    dataset_sha256: str
    config_sha256: str
    repo_revision: str
    pins_sha256: str

    def __post_init__(self) -> None:
        _label(self.campaign_id, field="campaign_id")
        if self.model != "gpt-5.6-luna":
            raise CampaignLedgerError("campaign binding must use gpt-5.6-luna")
        if self.reasoning_effort != "medium":
            raise CampaignLedgerError("campaign binding must use medium reasoning")
        for name in ("dataset_sha256", "config_sha256", "pins_sha256"):
            _digest(getattr(self, name), field=name)
        if not isinstance(self.repo_revision, str) or not self.repo_revision.strip():
            raise CampaignLedgerError("repo_revision must be non-empty")

    def public_fields(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetApproval:
    """Hash-bound approval artifact and conservative integer-microdollar prices."""

    max_calls: int
    max_cost_micros: int
    input_micros_per_million: int = 200_000
    cached_input_micros_per_million: int = 20_000
    cache_write_micros_per_million: int = 250_000
    output_micros_per_million: int = 1_200_000
    artifact_sha256: str = ""
    approved: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_calls", "max_cost_micros", "input_micros_per_million",
            "cached_input_micros_per_million", "cache_write_micros_per_million",
            "output_micros_per_million",
        ):
            _nonnegative_int(getattr(self, name), field=name)
        _positive_int(self.max_calls, field="max_calls")
        _positive_int(self.max_cost_micros, field="max_cost_micros")
        if not isinstance(self.approved, bool) or not self.approved:
            raise CampaignLedgerError("budget approval must be explicitly approved")
        if self.artifact_sha256:
            _digest(self.artifact_sha256, field="artifact_sha256")
            if self.artifact_sha256 != sha256_json(self.unsigned_fields()):
                raise CampaignLedgerError("budget approval artifact hash mismatch")

    def unsigned_fields(self) -> Dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_cost_micros": self.max_cost_micros,
            "input_micros_per_million": self.input_micros_per_million,
            "cached_input_micros_per_million": self.cached_input_micros_per_million,
            "cache_write_micros_per_million": self.cache_write_micros_per_million,
            "output_micros_per_million": self.output_micros_per_million,
            "approved": self.approved,
        }

    def __hash_payload(self) -> Dict[str, Any]:
        return self.unsigned_fields()

    @classmethod
    def create(
        cls,
        *,
        max_calls: int,
        max_cost_micros: int,
        input_micros_per_million: int = 200_000,
        cached_input_micros_per_million: int = 20_000,
        cache_write_micros_per_million: int = 250_000,
        output_micros_per_million: int = 1_200_000,
    ) -> "BudgetApproval":
        unsigned = {
            "max_calls": max_calls,
            "max_cost_micros": max_cost_micros,
            "input_micros_per_million": input_micros_per_million,
            "cached_input_micros_per_million": cached_input_micros_per_million,
            "cache_write_micros_per_million": cache_write_micros_per_million,
            "output_micros_per_million": output_micros_per_million,
            "approved": True,
        }
        return cls(**unsigned, artifact_sha256=sha256_json(unsigned))

    @classmethod
    def from_artifact(cls, artifact: Mapping[str, Any]) -> "BudgetApproval":
        if not isinstance(artifact, Mapping):
            raise CampaignLedgerError("budget approval artifact must be an object")
        raw = dict(artifact)
        supplied = raw.pop("artifact_sha256", None)
        if not isinstance(supplied, str) or _SHA256.fullmatch(supplied) is None:
            raise CampaignLedgerError("budget approval artifact requires artifact_sha256")
        if sha256_json(raw) != supplied:
            raise CampaignLedgerError("budget approval artifact hash mismatch")
        try:
            result = cls(**raw, artifact_sha256=supplied)
        except (TypeError, ValueError) as exc:
            raise CampaignLedgerError("budget approval artifact fields are invalid") from exc
        return result

    def public_fields(self) -> Dict[str, Any]:
        return {**self.unsigned_fields(), "artifact_sha256": self.artifact_sha256}


@dataclass(frozen=True)
class CallReservation:
    call_id: str
    kind: str
    status: str
    estimated_cost_micros: int
    response: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    response_sha256: Optional[str] = None
    error_class: Optional[str] = None
    request_sha256: Optional[str] = None


class _FileLock:
    """Small cross-process lock compatible with POSIX and Windows test hosts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
            os.fsync(self.handle.fileno())
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                if fcntl is None:
                    raise OSError("POSIX file locking is unavailable")
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise CampaignLedgerError("campaign ledger lock is unavailable") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class CampaignLedger:
    """Append-only reservation ledger with restart and process safety."""

    def __init__(
        self,
        path: str | Path,
        binding: CampaignBinding,
        approval: BudgetApproval,
    ) -> None:
        self.path = Path(path).expanduser()
        self.binding = binding
        self.approval = approval
        if not approval.artifact_sha256:
            raise CampaignLedgerError(
                "campaign ledger requires a hash-bound budget approval artifact"
            )
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _FileLock(self._lock_path):
            events = self._load_events()
            if not events:
                self._append_locked({
                    "schema_version": SCHEMA_VERSION,
                    "kind": "header",
                    "binding": self.binding.public_fields(),
                    "approval": self.approval.public_fields(),
                })
            else:
                self._validate_header(events[0])

    def _validate_header(self, event: Mapping[str, Any]) -> None:
        if event.get("schema_version") != SCHEMA_VERSION or event.get("kind") != "header":
            raise CampaignLedgerError("campaign ledger schema mismatch")
        try:
            binding = CampaignBinding(**dict(event["binding"]))
            approval = BudgetApproval(**dict(event["approval"]))
        except (KeyError, TypeError, CampaignLedgerError) as exc:
            raise CampaignLedgerError("campaign ledger header is invalid") from exc
        if binding != self.binding or approval != self.approval:
            raise CampaignLedgerError("campaign ledger binding or approval mismatch")

    def _load_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CampaignLedgerError("campaign ledger is unreadable") from exc
        events: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CampaignLedgerError(f"campaign ledger line {number} is invalid") from exc
            if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION:
                raise CampaignLedgerError(f"campaign ledger line {number} has a schema mismatch")
            events.append(event)
        return events

    def _append_locked(self, event: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise CampaignLedgerError("campaign ledger is unwritable") from exc

    def _state(self, events: list[dict[str, Any]]) -> Dict[str, CallReservation]:
        if not events:
            raise CampaignLedgerError("campaign ledger has no header")
        self._validate_header(events[0])
        state: Dict[str, CallReservation] = {}
        for event in events[1:]:
            kind = event.get("kind")
            call_id = event.get("call_id")
            if not isinstance(call_id, str):
                raise CampaignLedgerError("campaign ledger call id is invalid")
            prior = state.get(call_id)
            call_kind = event.get("call_kind")
            if call_kind not in CALL_KINDS:
                raise CampaignLedgerError("campaign ledger call kind is invalid")
            cost = _nonnegative_int(
                event.get("estimated_cost_micros", 0), field="estimated_cost_micros"
            )
            if kind == "reserved":
                if prior is not None:
                    raise CampaignLedgerError("campaign ledger contains duplicate reservation")
                request_sha256 = event.get("request_sha256")
                _digest(request_sha256, field="request_sha256")
                state[call_id] = CallReservation(
                    call_id, call_kind, "reserved", cost,
                    request_sha256=request_sha256,
                )
            elif prior is None:
                raise CampaignLedgerError("campaign ledger event has no reservation")
            elif prior.kind != call_kind:
                raise CampaignLedgerError("campaign ledger call kind changed mid-call")
            elif cost != prior.estimated_cost_micros:
                raise CampaignLedgerError("campaign ledger reservation cost changed mid-call")
            elif kind == "dispatched":
                if prior.status != "reserved":
                    raise CampaignLedgerError("campaign ledger dispatch transition is invalid")
                state[call_id] = CallReservation(
                    call_id, call_kind, "dispatched", cost,
                    request_sha256=prior.request_sha256,
                )
            elif kind == "completed":
                if prior.status not in {"reserved", "dispatched"}:
                    raise CampaignLedgerError("campaign ledger completion transition is invalid")
                response = _normalized_response(event.get("response", ""))
                usage = event.get("usage")
                if not isinstance(usage, dict):
                    raise CampaignLedgerError("campaign ledger usage is invalid")
                for usage_key, usage_value in usage.items():
                    if (not isinstance(usage_key, str)
                            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", usage_key)):
                        raise CampaignLedgerError("campaign ledger usage field is invalid")
                    if isinstance(usage_value, bool) or not isinstance(
                        usage_value, (int, float, str)
                    ):
                        raise CampaignLedgerError("campaign ledger usage value is invalid")
                    if isinstance(usage_value, (int, float)) and (
                        not math.isfinite(float(usage_value)) or float(usage_value) < 0
                    ):
                        raise CampaignLedgerError("campaign ledger usage number is invalid")
                    if isinstance(usage_value, str) and (
                        "\x00" in usage_value or len(usage_value) > 512
                    ):
                        raise CampaignLedgerError("campaign ledger usage string is invalid")
                response_sha256 = event.get("response_sha256")
                _digest(response_sha256, field="response_sha256")
                if response_sha256 != sha256_text(response):
                    raise CampaignLedgerError("campaign ledger response hash mismatch")
                actual_cost = _nonnegative_int(
                    event.get("actual_cost_micros", 0), field="actual_cost_micros"
                )
                if actual_cost > prior.estimated_cost_micros:
                    raise CampaignLedgerError(
                        "campaign ledger actual cost exceeds reservation"
                    )
                state[call_id] = CallReservation(
                    call_id, call_kind, "completed", cost, response=response,
                    usage=dict(usage), response_sha256=response_sha256,
                    request_sha256=prior.request_sha256,
                )
            elif kind == "uncertain":
                if prior.status not in {"reserved", "dispatched"}:
                    raise CampaignLedgerError("campaign ledger uncertainty transition is invalid")
                state[call_id] = CallReservation(
                    call_id, call_kind, "uncertain", cost,
                    error_class=str(event.get("error_class", "provider_uncertain")),
                    request_sha256=prior.request_sha256,
                )
            elif kind == "failed":
                if prior.status not in {"reserved", "dispatched"}:
                    raise CampaignLedgerError("campaign ledger failure transition is invalid")
                state[call_id] = CallReservation(
                    call_id, call_kind, "failed", cost,
                    error_class=str(event.get("error_class", "provider_error")),
                    request_sha256=prior.request_sha256,
                )
            else:
                raise CampaignLedgerError("campaign ledger event kind is invalid")
        return state

    def _validate_call(self, call_id: str, call_kind: str) -> None:
        _label(call_id, field="call_id")
        if call_kind not in CALL_KINDS:
            raise CampaignLedgerError("call_kind must be ingest, reader, evaluator, or correction")

    def reserve(
        self,
        call_id: str,
        call_kind: str,
        estimated_cost_micros: int,
        *,
        request_sha256: str,
    ) -> CallReservation:
        """Reserve budget before dispatch, or return a durable prior outcome."""
        self._validate_call(call_id, call_kind)
        cost = _nonnegative_int(estimated_cost_micros, field="estimated_cost_micros")
        _digest(request_sha256, field="request_sha256")
        with _FileLock(self._lock_path):
            events = self._load_events()
            state = self._state(events)
            prior = state.get(call_id)
            if prior is not None:
                if prior.request_sha256 != request_sha256:
                    raise CampaignLedgerError(
                        "call id is already bound to a different request"
                    )
                if prior.status == "completed":
                    return prior
                if prior.status == "uncertain":
                    return prior
                if prior.status == "failed":
                    return prior
                # A process died after reservation or dispatch.  It is unsafe to
                # guess whether the provider saw the request, so make it terminal.
                self._append_locked({
                    "schema_version": SCHEMA_VERSION,
                    "kind": "uncertain",
                    "call_id": call_id,
                    "call_kind": prior.kind,
                    "estimated_cost_micros": prior.estimated_cost_micros,
                    "error_class": "interrupted_before_completion",
                })
                return CallReservation(
                    call_id, prior.kind, "uncertain", prior.estimated_cost_micros,
                    error_class="interrupted_before_completion",
                    request_sha256=prior.request_sha256,
                )
            reserved = sum(item.estimated_cost_micros for item in state.values())
            if len(state) + 1 > self.approval.max_calls:
                raise BudgetExceeded("approved call ceiling would be exceeded")
            if reserved + cost > self.approval.max_cost_micros:
                raise BudgetExceeded("approved cash ceiling would be exceeded")
            self._append_locked({
                "schema_version": SCHEMA_VERSION,
                "kind": "reserved",
                "call_id": call_id,
                "call_kind": call_kind,
                "estimated_cost_micros": cost,
                "request_sha256": request_sha256,
            })
            return CallReservation(
                call_id, call_kind, "reserved", cost,
                request_sha256=request_sha256,
            )

    def mark_dispatched(self, call_id: str) -> None:
        _label(call_id, field="call_id")
        with _FileLock(self._lock_path):
            state = self._state(self._load_events())
            prior = state.get(call_id)
            if prior is None or prior.status != "reserved":
                raise CampaignLedgerError("call is not reserved for dispatch")
            self._append_locked({
                "schema_version": SCHEMA_VERSION,
                "kind": "dispatched",
                "call_id": call_id,
                "call_kind": prior.kind,
                "estimated_cost_micros": prior.estimated_cost_micros,
            })

    def complete(
        self,
        call_id: str,
        response: str,
        *,
        usage: Mapping[str, Any],
        actual_cost_micros: int,
    ) -> None:
        _label(call_id, field="call_id")
        normalized = _normalized_response(response)
        actual = _nonnegative_int(actual_cost_micros, field="actual_cost_micros")
        if not isinstance(usage, Mapping):
            raise CampaignLedgerError("usage must be an object")
        safe_usage: Dict[str, Any] = {}
        for key, value in usage.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise CampaignLedgerError("usage field name is invalid")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise CampaignLedgerError("usage contains an unsafe value")
            if isinstance(value, (int, float)) and (
                not math.isfinite(float(value)) or float(value) < 0
            ):
                raise CampaignLedgerError("usage contains an invalid number")
            if isinstance(value, str):
                if "\x00" in value or len(value) > 512:
                    raise CampaignLedgerError("usage contains an unsafe string")
                value = " ".join(value.split())
            safe_usage[key] = value
        with _FileLock(self._lock_path):
            state = self._state(self._load_events())
            prior = state.get(call_id)
            if prior is None or prior.status not in {"reserved", "dispatched"}:
                raise CampaignLedgerError("call is not pending completion")
            if actual > prior.estimated_cost_micros:
                raise CampaignLedgerError(
                    "actual call cost exceeds its durable reservation"
                )
            self._append_locked({
                "schema_version": SCHEMA_VERSION,
                "kind": "completed",
                "call_id": call_id,
                "call_kind": prior.kind,
                "estimated_cost_micros": prior.estimated_cost_micros,
                "actual_cost_micros": actual,
                "response": normalized,
                "response_sha256": sha256_text(normalized),
                "usage": safe_usage,
            })

    def mark_uncertain(self, call_id: str, *, error_class: str = "provider_uncertain") -> None:
        self._mark_terminal(call_id, "uncertain", error_class)

    def mark_failed(self, call_id: str, *, error_class: str = "provider_error") -> None:
        self._mark_terminal(call_id, "failed", error_class)

    def _mark_terminal(self, call_id: str, kind: str, error_class: str) -> None:
        _label(call_id, field="call_id")
        safe_error = str(error_class).strip().casefold()
        if _LABEL.fullmatch(safe_error) is None:
            safe_error = "provider_uncertain" if kind == "uncertain" else "provider_error"
        with _FileLock(self._lock_path):
            state = self._state(self._load_events())
            prior = state.get(call_id)
            if prior is None or prior.status not in {"reserved", "dispatched"}:
                raise CampaignLedgerError("call is not pending terminal transition")
            self._append_locked({
                "schema_version": SCHEMA_VERSION,
                "kind": kind,
                "call_id": call_id,
                "call_kind": prior.kind,
                "estimated_cost_micros": prior.estimated_cost_micros,
                "error_class": safe_error,
            })

    def lookup(self, call_id: str) -> Optional[CallReservation]:
        _label(call_id, field="call_id")
        with _FileLock(self._lock_path):
            return self._state(self._load_events()).get(call_id)

    def summary(self) -> Dict[str, Any]:
        with _FileLock(self._lock_path):
            state = self._state(self._load_events())
            reserved = sum(item.estimated_cost_micros for item in state.values())
            by_status: Dict[str, int] = {}
            for item in state.values():
                by_status[item.status] = by_status.get(item.status, 0) + 1
            return {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": self.binding.campaign_id,
                "model": self.binding.model,
                "reasoning_effort": self.binding.reasoning_effort,
                "calls": len(state),
                "reserved_cost_micros": reserved,
                "max_calls": self.approval.max_calls,
                "max_cost_micros": self.approval.max_cost_micros,
                "by_status": by_status,
            }

    def __enter__(self) -> "CampaignLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


__all__ = [
    "BudgetApproval", "BudgetExceeded", "CALL_KINDS", "CampaignBinding",
    "CampaignLedger", "CampaignLedgerError", "CallReservation", "SCHEMA_VERSION",
    "UncertainCall", "canonical_json", "sha256_json", "sha256_text",
]
