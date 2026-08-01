"""Private, resumable checkpoint ledger for hosted benchmark attempts.

The ledger deliberately records only stable protocol identifiers and bounded,
normalized answers.  Prompts, contexts, questions, task IDs, credentials, and
provider error text never enter its JSONL records.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Union


SCHEMA_VERSION = "engraphis-hosted-ledger/1"
MAX_NORMALIZED_ANSWER_CHARS = 4_096
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_ERROR_CLASS = re.compile(r"[a-z][a-z0-9_-]{0,63}")
# Test runners may need a repo-local base temp directory when the system temp location
# is unavailable.  Keep that narrowly scoped to explicitly ignored top-level directories;
# normal repo-local private records still belong only in .private-eval.
_TEMPORARY_REPO_DIR = re.compile(r"\.tmp[-_][a-zA-Z0-9][a-zA-Z0-9_.-]*")


class HostedLedgerError(ValueError):
    """A checkpoint validation error safe to show without provider details."""


def _sha256(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise HostedLedgerError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _label(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise HostedLedgerError(f"{field} must be a bounded lowercase label")
    return value


def _ordinal(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HostedLedgerError(f"{field} must be a non-negative integer")
    return value


def normalize_answer(answer: str) -> str:
    """Return a bounded answer suitable for private resume/scoring only.

    Normalization keeps checkpoint matching stable without preserving arbitrary
    model formatting.  Oversized responses fail closed rather than truncating a
    value that could change deterministic scoring after a resume.
    """
    if not isinstance(answer, str):
        raise HostedLedgerError("normalized answer must be a string")
    normalized = " ".join(unicodedata.normalize("NFC", answer).split())
    if len(normalized) > MAX_NORMALIZED_ANSWER_CHARS:
        raise HostedLedgerError("normalized answer exceeds the private ledger size cap")
    return normalized


@dataclass(frozen=True)
class RunBinding:
    """Immutable identity shared by every record in one hosted benchmark run."""

    model: str
    dataset_sha256: str
    config_sha256: str
    repo_revision: str
    repo_dirty: bool
    repo_dirty_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise HostedLedgerError("model must be a non-empty bounded string")
        _sha256(self.dataset_sha256, field="dataset_sha256")
        _sha256(self.config_sha256, field="config_sha256")
        _sha256(self.repo_dirty_sha256, field="repo_dirty_sha256")
        if not isinstance(self.repo_revision, str) or not self.repo_revision.strip() or (
                len(self.repo_revision) > 128):
            raise HostedLedgerError("repo_revision must be a non-empty bounded string")
        if not isinstance(self.repo_dirty, bool):
            raise HostedLedgerError("repo_dirty must be boolean")

    def public_fields(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AttemptIdentity:
    """One opaque, ordinal-addressed hosted turn within a bound benchmark run."""

    repetition: int
    strategy: str
    task_ordinal: int
    turn_ordinal: int

    def __post_init__(self) -> None:
        _ordinal(self.repetition, field="repetition")
        _label(self.strategy, field="strategy")
        _ordinal(self.task_ordinal, field="task_ordinal")
        _ordinal(self.turn_ordinal, field="turn_ordinal")

    @property
    def key(self) -> str:
        return "{0}:{1}:{2}:{3}".format(
            self.repetition, self.strategy, self.task_ordinal, self.turn_ordinal
        )

    def public_fields(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CheckpointTurn:
    """Minimal completed-turn data allowed back into a resumed scorer."""

    answer: str
    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_ms: Optional[float] = None

    def __post_init__(self) -> None:
        normalize_answer(self.answer)
        for field in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or value < 0):
                raise HostedLedgerError(f"{field} must be a non-negative integer or null")
        if self.latency_ms is not None and (
                isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float))
                or not math.isfinite(float(self.latency_ms)) or self.latency_ms < 0):
            raise HostedLedgerError("latency_ms must be non-negative or null")

    def public_fields(self) -> dict:
        data = asdict(self)
        data["answer"] = normalize_answer(data["answer"])
        return data


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _temporary_repo_root(lexical: Path, root: Path) -> Optional[Path]:
    """Return a safe, ignored repo-local test root, if *lexical* is inside one.

    ``--basetemp=.tmp-pytest`` is a practical fallback on locked-down Windows
    installations.  It must not weaken the normal ``.private-eval`` policy or let a
    symlink turn a repo-local-looking path into an arbitrary destination.
    """

    try:
        relative = lexical.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or not _TEMPORARY_REPO_DIR.fullmatch(relative.parts[0]):
        return None
    temporary_root = root / relative.parts[0]
    # A temporary root must be a real child of the repository.  This rejects a symlink
    # or junction at the root and the final containment check below rejects nested links.
    if temporary_root.resolve(strict=False) != temporary_root:
        return None
    return temporary_root


def resolve_private_ledger_path(
    path: Union[str, Path], *, repo_root: Optional[Union[str, Path]] = None,
) -> Path:
    """Allow repo-local checkpoints only in ``.private-eval``, or an absolute external path.

    The lexical and resolved paths are both checked so a symlink cannot turn a
    repo-local-looking checkpoint into an arbitrary tracked path (or vice versa).
    """
    root = Path(repo_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    raw = Path(path).expanduser()
    lexical = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(str(lexical)))
    resolved = lexical.resolve(strict=False)
    private_root = (root / ".private-eval").resolve(strict=False)

    if _inside(lexical, root):
        temporary_root = _temporary_repo_root(lexical, root)
        if not _inside(resolved, private_root) and not (
            temporary_root is not None and _inside(resolved, temporary_root)
        ):
            raise HostedLedgerError(
                "repo-local private records must resolve under .private-eval or an ignored .tmp-* directory"
            )
    else:
        if not raw.is_absolute():
            raise HostedLedgerError("outside-repo private records require an absolute path")
        if _inside(resolved, root):
            raise HostedLedgerError("external private record path resolves into the repository")
    return resolved


class PrivateHostedLedger:
    """Append-only local ledger with duplicate protection and a persisted call budget."""

    def __init__(
        self,
        path: Union[str, Path],
        binding: RunBinding,
        *,
        repo_root: Optional[Union[str, Path]] = None,
    ) -> None:
        self.path = resolve_private_ledger_path(path, repo_root=repo_root)
        self.binding = binding
        self.completed: dict[str, CheckpointTurn] = {}
        self.calls_started = 0
        self._attempt_state: dict[str, str] = {}
        self._lock_handle = None
        self._acquire_lock()
        try:
            if self.path.exists():
                self._load()
        except Exception:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        handle = lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise HostedLedgerError(
                "another hosted benchmark process already holds this private ledger"
            ) from exc
        self._lock_handle = handle

    def close(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_handle = None

    def __enter__(self) -> "PrivateHostedLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _base_record(self, identity: AttemptIdentity, *, kind: str) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            **self.binding.public_fields(),
            **identity.public_fields(),
            "attempt_key": identity.key,
            "calls_started": self.calls_started,
        }

    def _load(self) -> None:
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HostedLedgerError(f"private ledger line {number} is invalid") from exc
            self._validate_loaded_record(record, number=number)
            kind, key, calls = record["kind"], record["attempt_key"], record["calls_started"]
            if kind == "call_started":
                if calls != self.calls_started + 1:
                    raise HostedLedgerError("private ledger call count is not monotonic")
                previous = self._attempt_state.get(key)
                if previous not in {None, "retry"}:
                    raise HostedLedgerError(
                        "private ledger contains an invalid hosted-attempt transition"
                    )
                self.calls_started = calls
                self._attempt_state[key] = "started"
            else:
                if kind == "completed" and key in self.completed:
                    raise HostedLedgerError(
                        "private ledger contains a duplicate completed attempt"
                    )
                if calls != self.calls_started or self._attempt_state.get(key) != "started":
                    raise HostedLedgerError("private ledger event has no reserved hosted call")
                self._attempt_state[key] = kind
            if kind == "completed":
                self.completed[key] = CheckpointTurn(**record["turn"])

    def _validate_loaded_record(self, record: object, *, number: int) -> None:
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise HostedLedgerError(f"private ledger line {number} has a different schema")
        try:
            stored = RunBinding(
                model=record["model"], dataset_sha256=record["dataset_sha256"],
                config_sha256=record["config_sha256"], repo_revision=record["repo_revision"],
                repo_dirty=record["repo_dirty"], repo_dirty_sha256=record["repo_dirty_sha256"],
            )
            identity = AttemptIdentity(
                repetition=record["repetition"], strategy=record["strategy"],
                task_ordinal=record["task_ordinal"], turn_ordinal=record["turn_ordinal"],
            )
            calls = _ordinal(record["calls_started"], field="calls_started")
        except (KeyError, TypeError, HostedLedgerError) as exc:
            raise HostedLedgerError(f"private ledger line {number} has an invalid record") from exc
        if stored != self.binding:
            raise HostedLedgerError("private ledger belongs to another benchmark binding")
        if record.get("attempt_key") != identity.key or record.get("kind") not in {
                "call_started", "retry", "failure", "completed"}:
            raise HostedLedgerError(f"private ledger line {number} has an invalid record kind")
        if record["kind"] == "completed":
            try:
                CheckpointTurn(**record["turn"])
            except (KeyError, TypeError, HostedLedgerError) as exc:
                raise HostedLedgerError(
                    f"private ledger line {number} has an invalid completed turn"
                ) from exc
        elif record.get("turn") is not None:
            raise HostedLedgerError(f"private ledger line {number} stores turn data for an event")
        if record["kind"] in {"retry", "failure"}:
            _error_class(record.get("error_class"))
        elif "error_class" in record:
            raise HostedLedgerError(f"private ledger line {number} has an unexpected error class")
        if calls < 0:
            raise HostedLedgerError(f"private ledger line {number} has an invalid call count")

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def reserve_call(self, identity: AttemptIdentity, *, max_calls: int) -> int:
        """Durably reserve one provider call before it starts, across restarts."""
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
            raise HostedLedgerError("max_calls must be a positive integer")
        state = self._attempt_state.get(identity.key)
        if state == "completed":
            raise HostedLedgerError("completed attempts cannot start another hosted call")
        if state == "failure":
            raise HostedLedgerError("failed attempts are terminal for this benchmark binding")
        if state == "started":
            raise HostedLedgerError(
                "interrupted hosted attempts are terminal for this benchmark binding"
            )
        if self.calls_started >= max_calls:
            raise HostedLedgerError("hosted call ceiling would be exceeded")
        self.calls_started += 1
        self._append(self._base_record(identity, kind="call_started"))
        self._attempt_state[identity.key] = "started"
        return self.calls_started

    def append_retry(self, identity: AttemptIdentity, *, error_class: str) -> None:
        self._require_reserved(identity)
        record = self._base_record(identity, kind="retry")
        record["error_class"] = _error_class(error_class)
        self._append(record)
        self._attempt_state[identity.key] = "retry"

    def append_failure(self, identity: AttemptIdentity, *, error_class: str) -> None:
        self._require_reserved(identity)
        record = self._base_record(identity, kind="failure")
        record["error_class"] = _error_class(error_class)
        self._append(record)
        self._attempt_state[identity.key] = "failure"

    def append_completed(self, identity: AttemptIdentity, turn: CheckpointTurn) -> None:
        if identity.key in self.completed:
            raise HostedLedgerError("private ledger already contains this completed attempt")
        self._require_reserved(identity)
        normalized_turn = CheckpointTurn(**turn.public_fields())
        record = self._base_record(identity, kind="completed")
        record["turn"] = normalized_turn.public_fields()
        self._append(record)
        self.completed[identity.key] = normalized_turn
        self._attempt_state[identity.key] = "completed"

    def resume(self, identity: AttemptIdentity) -> Optional[CheckpointTurn]:
        """Return only an exact completed ordinal, never a fuzzy prompt match."""
        return self.completed.get(identity.key)

    def _require_reserved(self, identity: AttemptIdentity) -> None:
        if self._attempt_state.get(identity.key) != "started":
            raise HostedLedgerError("hosted attempt has no reserved call")


def _error_class(value: object) -> str:
    if not isinstance(value, str) or not _ERROR_CLASS.fullmatch(value):
        raise HostedLedgerError("error_class must be a bounded lowercase label")
    return value


def text_sha256(value: str) -> str:
    """Canonical helper for callers that need a binding digest without storing text."""
    if not isinstance(value, str):
        raise HostedLedgerError("digest input must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
