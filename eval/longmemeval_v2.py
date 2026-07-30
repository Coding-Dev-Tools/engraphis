"""Adapter for LongMemEval-V2's official ``Memory.insert/query`` contract.

The official harness owns downloading data, model calls, and scoring.  This
adapter deliberately does none of those things: it exposes an Engraphis-backed
memory object with the same two public methods for use inside that harness.
"""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Optional, Protocol, Sequence, Union

from engraphis.core.context import RegexTokenCounter
from engraphis.service import MemoryService
from eval.benchmark import CANONICAL_TOKEN_BUDGETS


_PINNED_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_MAX_TRAJECTORY_CHUNK_CHARS = 1800


def _require_configured_embedder(service: MemoryService, model: Optional[str],
                                 revision: Optional[str]) -> None:
    """Fail closed when a benchmark-requested embedder silently fell back.

    The general Engraphis factory intentionally degrades to its offline deterministic
    embedder when an optional model cannot load. That is useful product behavior but
    invalid for a canonical benchmark: the resulting artifact would otherwise name a
    Qwen revision that never produced its vectors.
    """
    if not model:
        return
    engine = getattr(service, "engine", None)
    embedder = getattr(engine, "embedder", None)
    if (
        getattr(embedder, "model_name", None) != model
        or getattr(embedder, "revision", None) != revision
    ):
        raise RuntimeError(
            "the configured benchmark embedder did not load at its pinned revision; "
            "canonical fallback is forbidden"
        )


# LongMemEval-V2 commit 6f020ac2fc3275e46c706d3406e02c3ed79b7be2
# exposes this interface as ``memory_modules.memory``.  It is an optional
# benchmark dependency: importing this module must continue to work in the
# normal offline Engraphis test environment.
try:  # pragma: no cover - exercised in an isolated fake-official-package test
    from memory_modules.memory import Memory as _MemoryBase
    from memory_modules.memory import register_memory as _register_memory
    OFFICIAL_MEMORY_AVAILABLE = True
except Exception:  # noqa: BLE001 - third-party optional import boundary
    OFFICIAL_MEMORY_AVAILABLE = False

    class _MemoryBase:
        """Small compatible fallback for local/offline use only."""

        memory_type = ""

        def __init__(self, memory_params: dict[str, object]) -> None:
            self.memory_params = dict(memory_params)

        @property
        def memory_config(self) -> dict[str, object]:
            return {"memory_type": self.memory_type, "memory_params": self.memory_params}

        def configure_runtime(self, **kwargs: object) -> None:
            del kwargs

        def save_memory(self, output_dir: str | Path) -> None:
            path = Path(output_dir)
            path.mkdir(parents=True, exist_ok=True)
            (path / "memory_config.json").write_text(
                json.dumps(self.memory_config, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            self._save_backend(path)

        def _save_backend(self, output_dir: Path) -> None:
            del output_dir

        def _load_backend(self, input_dir: Path) -> None:
            del input_dir

    def _register_memory(memory_cls):
        return memory_cls


class ContextTokenizer(Protocol):
    """Small tokenizer surface accepted by the official-harness adapter."""

    def encode(self, text: str) -> Sequence[Any]:
        ...


def _reader_tokenizer_identity(model: str, revision: str) -> str:
    """Return the immutable identity recorded for canonical reader accounting."""
    return f"{model}@{revision}"


def _load_pinned_reader_tokenizer(model: str, revision: str) -> ContextTokenizer:
    """Load the official reader tokenizer only for an explicitly canonical run.

    Importing transformers stays optional for the offline core.  Passing the
    immutable revision to ``from_pretrained`` is important: a mutable model tag
    would make a token budget unverifiable even if its displayed model name was
    unchanged.
    """
    try:
        # The official V2 harness builds prompts with ``AutoProcessor`` rather
        # than loading a tokenizer directly.  Use that exact public surface at
        # the pinned revision and take its text tokenizer for preflight context
        # accounting.  This keeps the adapter's hard budget aligned with the
        # reader path instead of merely using a similarly named tokenizer.
        from transformers import AutoProcessor
    except ImportError as exc:  # pragma: no cover - depends on optional benchmark install
        raise ValueError(
            "canonical LongMemEval-V2 accounting requires transformers and the pinned "
            "official reader processor/tokenizer"
        ) from exc
    processor = AutoProcessor.from_pretrained(model, revision=revision)
    tokenizer = getattr(processor, "tokenizer", processor)
    if not hasattr(tokenizer, "encode"):
        raise ValueError(
            "canonical LongMemEval-V2 reader processor did not expose an encode-capable tokenizer"
        )
    return tokenizer


@_register_memory
class EngraphisLongMemEvalV2Memory(_MemoryBase):
    """Minimal official-harness-compatible memory backend.

    ``insert`` accepts a full trajectory object. ``query`` returns the official
    list of ``{"type": "text", "value": ...}`` context items. Image handling
    remains the official harness's concern; trajectory text is indexed locally.
    """

    memory_type = "engraphis"

    def __init__(
        self,
        memory_params: Optional[dict[str, object]] = None,
        *,
        context_k: Optional[int] = None,
        max_context_tokens: Optional[int] = None,
        tokenizer: Optional[Union[ContextTokenizer, Callable[[str], int]]] = None,
        tokenizer_identity: Optional[str] = None,
        require_exact_reader_tokenizer: Optional[bool] = None,
        reader_tokenizer_model: Optional[str] = None,
        reader_tokenizer_revision: Optional[str] = None,
        retrieval_profile: Optional[str] = None,
        embed_model: Optional[str] = None,
        embed_revision: Optional[str] = None,
        vector_backend: Optional[str] = None,
        service: Optional[MemoryService] = None,
    ) -> None:
        """Construct directly or from official ``memory_params`` configuration.

        The official ``build_memory`` calls ``memory_cls(memory_params)``.  The
        keyword form is retained for local tests and programmatic integrations.
        Runtime-only dependencies such as a tokenizer or an already-open service
        deliberately do not enter the persisted config.
        """
        params = dict(memory_params or {})
        unknown = set(params) - {
            "context_k", "max_context_tokens", "tokenizer_identity", "retrieval_profile",
            "embed_model", "embed_revision", "vector_backend", "require_exact_reader_tokenizer",
            "reader_tokenizer_model", "reader_tokenizer_revision",
        }
        if unknown:
            raise ValueError("unsupported Engraphis LongMemEval memory_params: " + ", ".join(sorted(unknown)))
        resolved_context_k = context_k if context_k is not None else params.get("context_k", 8)
        resolved_max_tokens = (
            max_context_tokens if max_context_tokens is not None
            else params.get("max_context_tokens", CANONICAL_TOKEN_BUDGETS[2])
        )
        resolved_profile = (
            retrieval_profile if retrieval_profile is not None
            else params.get("retrieval_profile", "balanced")
        )
        self.context_k = max(1, int(resolved_context_k))
        self.max_context_tokens = int(resolved_max_tokens)
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self.require_exact_reader_tokenizer = bool(
            require_exact_reader_tokenizer
            if require_exact_reader_tokenizer is not None
            else params.get("require_exact_reader_tokenizer", False)
        )
        self.reader_tokenizer_model = str(
            reader_tokenizer_model
            if reader_tokenizer_model is not None
            else params.get("reader_tokenizer_model") or ""
        ).strip() or None
        self.reader_tokenizer_revision = str(
            reader_tokenizer_revision
            if reader_tokenizer_revision is not None
            else params.get("reader_tokenizer_revision") or ""
        ).strip() or None
        if self.require_exact_reader_tokenizer:
            if not self.reader_tokenizer_model or not self.reader_tokenizer_revision:
                raise ValueError(
                    "canonical LongMemEval-V2 accounting requires reader_tokenizer_model "
                    "and reader_tokenizer_revision"
                )
            if _PINNED_REVISION.fullmatch(self.reader_tokenizer_revision) is None:
                raise ValueError(
                    "reader_tokenizer_revision must be an immutable lowercase "
                    "40-character commit"
                )
            expected_tokenizer_identity = _reader_tokenizer_identity(
                self.reader_tokenizer_model, self.reader_tokenizer_revision
            )
            declared_identity = tokenizer_identity or params.get("tokenizer_identity")
            if declared_identity != expected_tokenizer_identity:
                raise ValueError(
                    "canonical LongMemEval-V2 accounting requires the pinned reader "
                    "tokenizer identity"
                )
            if tokenizer is not None:
                raise ValueError(
                    "canonical LongMemEval-V2 accounting loads the pinned reader tokenizer "
                    "internally and does not accept an injected replacement"
                )
            tokenizer = _load_pinned_reader_tokenizer(
                self.reader_tokenizer_model, self.reader_tokenizer_revision
            )
            tokenizer_identity = expected_tokenizer_identity
        self._tokenizer = tokenizer or RegexTokenCounter()
        self.tokenizer_identity = (
            tokenizer_identity
            or params.get("tokenizer_identity")
            or getattr(self._tokenizer, "identity", None)
            or getattr(self._tokenizer, "__name__", None)
            or type(self._tokenizer).__name__
        )
        self.retrieval_profile = str(resolved_profile or "balanced").strip().casefold()
        self.embed_model = str(
            embed_model if embed_model is not None else params.get("embed_model") or ""
        ).strip() or None
        self.embed_revision = str(
            embed_revision if embed_revision is not None else params.get("embed_revision") or ""
        ).strip() or None
        self.vector_backend = str(
            vector_backend if vector_backend is not None else params.get("vector_backend") or "numpy"
        ).strip().casefold()
        if self.embed_model and (
            self.embed_revision is None or _PINNED_REVISION.fullmatch(self.embed_revision) is None
        ):
            raise ValueError(
                "embed_revision must be an immutable lowercase 40-character commit "
                "when embed_model is configured"
            )
        if self.embed_revision and not self.embed_model:
            raise ValueError("embed_model is required when embed_revision is configured")
        if self.vector_backend not in {"numpy", "sqlite-vec"}:
            raise ValueError("vector_backend must be numpy or sqlite-vec")
        persisted_params = {
            "context_k": self.context_k,
            "max_context_tokens": self.max_context_tokens,
            "tokenizer_identity": self.tokenizer_identity,
            "require_exact_reader_tokenizer": self.require_exact_reader_tokenizer,
            "reader_tokenizer_model": self.reader_tokenizer_model,
            "reader_tokenizer_revision": self.reader_tokenizer_revision,
            "retrieval_profile": self.retrieval_profile,
            "embed_model": self.embed_model,
            "embed_revision": self.embed_revision,
            "vector_backend": self.vector_backend,
        }
        super().__init__(persisted_params)
        self.service = service or MemoryService.create(
            ":memory:",
            embed_model=self.embed_model,
            embed_revision=self.embed_revision,
            vector_backend=self.vector_backend,
        )
        _require_configured_embedder(
            self.service, self.embed_model, self.embed_revision
        )
        self.workspace = "longmemeval-v2"
        self.repo = "trajectory"
        self._counter = 0
        self._query_result_local = threading.local()

    def configure_runtime(self, **kwargs: object) -> None:
        """Accept an official-harness runtime tokenizer without changing config."""
        tokenizer = kwargs.pop("tokenizer", None)
        tokenizer_identity = kwargs.pop("tokenizer_identity", None)
        if self.require_exact_reader_tokenizer and (
            tokenizer is not None or tokenizer_identity is not None
        ):
            raise ValueError(
                "canonical LongMemEval-V2 accounting does not permit runtime tokenizer replacement"
            )
        if tokenizer is not None:
            if not callable(tokenizer) and not hasattr(tokenizer, "encode"):
                raise TypeError("tokenizer must be callable or expose encode()")
            self._tokenizer = tokenizer  # type: ignore[assignment]
        if tokenizer_identity is not None:
            self.tokenizer_identity = str(tokenizer_identity)
        elif tokenizer is not None:
            self.tokenizer_identity = (
                getattr(tokenizer, "identity", None)
                or getattr(tokenizer, "__name__", None)
                or type(tokenizer).__name__
            )
        super().configure_runtime(**kwargs)

    @property
    def metadata(self) -> dict[str, Any]:
        """Stable context-budget metadata for the official benchmark artifact."""
        return {
            "memory_type": self.memory_type,
            "context_k": self.context_k,
            "max_context_tokens": self.max_context_tokens,
            "budget_curve_status": "single_operating_point",
            "required_budget_matrix": list(CANONICAL_TOKEN_BUDGETS),
            "tokenizer": self.tokenizer_identity,
            "token_budget_method": (
                "pinned_reader_content_tokenizer"
                if self.require_exact_reader_tokenizer else "deterministic_estimate"
            ),
            "token_budget_scope": "per_context_item_content_excluding_prompt_framing",
            "retrieval_profile": self.retrieval_profile,
            "embed_model": self.embed_model or "deterministic",
            "embed_revision": self.embed_revision,
            "vector_backend": self.vector_backend,
            "response_mode": "compact",
        }

    def insert(self, trajectory: dict[str, Any]) -> None:
        """Store one official trajectory without assuming its private schema."""
        trajectory_id = str(trajectory.get("trajectory_id") or trajectory.get("id") or self._counter)
        segments = _trajectory_segments(trajectory)
        if not segments:
            return
        sequence = 0
        for state_index, text in segments:
            for chunk_index, chunk in enumerate(_split_trajectory_text(text), start=1):
                sequence += 1
                self.service.remember(
                    chunk,
                    workspace=self.workspace,
                    repo=self.repo,
                    mtype="episodic",
                    scope="repo",
                    title=f"trajectory:{trajectory_id}:state:{state_index}:part:{chunk_index}",
                    metadata={
                        "benchmark": "LongMemEval-V2",
                        "trajectory_id": trajectory_id,
                        "state_index": state_index,
                        "chunk_index": chunk_index,
                        "sequence": sequence,
                    },
                    source="benchmark",
                    kind="longmemeval_v2",
                    resolve_conflicts=False,
                )
        self._counter += 1

    def query(self, query: str, query_image: Optional[str] = None) -> list[dict]:
        """Return text context items accepted by LongMemEval-V2's reader harness."""
        del query_image  # The official protocol permits it; text-only retrieval is explicit.
        response = self.service.recall(
            query,
            workspace=self.workspace,
            repo=self.repo,
            k=self.context_k,
            token_budget=self.max_context_tokens,
            retrieval_profile=self.retrieval_profile,
            response_mode="compact",
            reinforce=False,
            # The official harness can build prompts concurrently.  Benchmark
            # retrieval is observational: receipt writes would add contention,
            # alter the SQLite database, and make repeated reader calls
            # non-deterministic without contributing benchmark evidence.
            record_receipt=False,
        )
        # The packed context is compact by construction.  Recount it under the
        # pinned reader tokenizer because the engine's local counter may be
        # different; clip once more to make the evidence-item content budget
        # exact. Prompt framing and inter-item separators remain the official
        # harness's responsibility and are deliberately not counted here.
        items = _context_items_with_budget(
            str(response.get("context") or ""),
            budget=self.max_context_tokens,
            count=self._count_tokens,
        )
        packed_sources = response.get("packed_sources", [])
        source_ids: list[str] = []
        if isinstance(packed_sources, list):
            for item in items:
                match = re.match(r"\s*\[(\d+)\]", str(item.get("value") or ""))
                if match is None:
                    continue
                source_index = int(match.group(1)) - 1
                if 0 <= source_index < len(packed_sources):
                    source_id = packed_sources[source_index].get("id")
                    if source_id:
                        source_ids.append(str(source_id))
        self._query_result_local.metadata = {
            "memory_type": self.memory_type,
            "retrieval_profile": response.get("retrieval_profile"),
            "source_ids": source_ids,
            "usage": response.get("usage", {}),
            "returned_context_tokens": sum(
                self._count_tokens(item["value"]) for item in items
            ),
            "returned_context_items": len(items),
            "tokenizer": self.tokenizer_identity,
            "token_budget_method": (
                "pinned_reader_content_tokenizer"
                if self.require_exact_reader_tokenizer else "deterministic_estimate"
            ),
            "token_budget_scope": "per_context_item_content_excluding_prompt_framing",
            "budget_curve_status": "single_operating_point",
        }
        return items

    def post_query_hook(
        self,
        *,
        query: str,
        query_image: Optional[str],
        memory_context: list[dict],
    ) -> dict[str, object]:
        """Expose content-free Engraphis retrieval evidence to official run logs."""
        del query, query_image, memory_context
        metadata = getattr(self._query_result_local, "metadata", None)
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _count_tokens(self, text: str) -> int:
        if callable(self._tokenizer) and not hasattr(self._tokenizer, "encode"):
            return max(0, int(self._tokenizer(text)))
        try:
            return len(self._tokenizer.encode(text, add_special_tokens=False))
        except TypeError:
            return len(self._tokenizer.encode(text))

    def _save_backend(self, output_dir: Path) -> None:
        """Persist the local SQLite store through the official memory hook."""
        database = output_dir / "engraphis.sqlite"
        target = sqlite3.connect(str(database))
        try:
            self.service.store.conn.backup(target)
        finally:
            target.close()
        (output_dir / "engraphis_state.json").write_text(
            json.dumps({"counter": self._counter}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _load_backend(self, input_dir: Path) -> None:
        """Restore a SQLite store saved by :meth:`_save_backend`."""
        database = input_dir / "engraphis.sqlite"
        if not database.is_file():
            raise FileNotFoundError(f"missing saved Engraphis SQLite backend: {database}")
        self.service.store.close()
        self.service = MemoryService.create(
            str(database),
            embed_model=self.embed_model,
            embed_revision=self.embed_revision,
            vector_backend=self.vector_backend,
        )
        _require_configured_embedder(
            self.service, self.embed_model, self.embed_revision
        )
        state = input_dir / "engraphis_state.json"
        if state.is_file():
            payload = json.loads(state.read_text(encoding="utf-8"))
            self._counter = int(payload.get("counter", 0))


def _fit_to_budget(text: str, budget: int, count: Callable[[str], int]) -> str:
    """Return a deterministic prefix whose injected-token count fits ``budget``."""
    text = text.strip()
    if not text or count(text) <= budget:
        return text
    # Prefix token counts are monotonic for normal reader tokenizers.  The final
    # loop remains a correctness guard for unusual injected implementations.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    clipped = text[:low].rstrip()
    while clipped and count(clipped) > budget:
        clipped = clipped[:-1].rstrip()
    return clipped


def _context_items_with_budget(
    context: str,
    *,
    budget: int,
    count: Callable[[str], int],
) -> list[dict[str, str]]:
    """Keep packed sources as separate official items under one aggregate budget.

    LongMemEval-V2 truncates context at item boundaries using the canonical
    Qwen reader processor. Returning one monolithic item would therefore turn a
    small tokenizer mismatch into *zero* evidence. Separate packed sources let
    the official harness retain the largest exact-token prefix while this
    adapter still enforces its configured counter exactly.
    """
    blocks = [
        block.strip()
        for block in re.split(r"\n{2,}(?=\[\d+\](?:\s|$))", context.strip())
        if block.strip()
    ]
    items: list[dict[str, str]] = []
    used = 0
    for block in blocks:
        remaining = budget - used
        if remaining <= 0:
            break
        fitted = _fit_to_budget(block, remaining, count)
        if not fitted:
            continue
        items.append({"type": "text", "value": fitted})
        used += count(fitted)
    return items


def _trajectory_text(trajectory: dict[str, Any]) -> str:
    """Flatten current official and legacy trajectory shapes without model calls."""
    return "\n".join(text for _index, text in _trajectory_segments(trajectory))


def _trajectory_segments(trajectory: dict[str, Any]) -> list[tuple[int, str]]:
    """Return ordered state text without retaining a duplicate full trajectory.

    Official V2 trajectories have a ``states`` list.  Indexing that whole list
    as one memory lets an embedder truncate late states, so each useful state is
    kept independently and then deterministically chunked by :meth:`insert`.
    Legacy list shapes follow the same rule.  Direct text is used only when no
    structured state is available, avoiding a summary/full-history duplicate.
    """
    segments: list[tuple[int, str]] = []
    for key in ("states", "content", "steps", "trajectory"):
        items = trajectory.get(key)
        if not isinstance(items, list):
            continue
        for state_index, item in enumerate(items):
            lines: list[str] = []
            _append_trajectory_item(lines, item)
            text = "\n".join(lines).strip()
            if text:
                segments.append((state_index, text))
        if segments:
            return segments
    for key in ("text", "trajectory_text", "notes"):
        value = trajectory.get(key)
        if isinstance(value, str) and value.strip():
            return [(0, value.strip())]
    content = trajectory.get("content")
    if isinstance(content, str) and content.strip():
        return [(0, content.strip())]
    return []


def _split_trajectory_text(text: str, *, max_chars: int = _MAX_TRAJECTORY_CHUNK_CHARS) -> list[str]:
    """Split long state text deterministically without dropping or repeating text."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    chunks: list[str] = []
    current = ""
    for line in lines:
        # A single accessibility-tree line can itself exceed an embedder's
        # useful input.  Preserve every character in stable fixed-size pieces.
        parts = [line[index:index + max_chars] for index in range(0, len(line), max_chars)]
        for part in parts:
            candidate = part if not current else current + "\n" + part
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = part
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def _append_trajectory_item(lines: list[str], item: Any) -> None:
    if isinstance(item, str):
        if item.strip():
            lines.append(item.strip())
        return
    if not isinstance(item, dict):
        return
    for key in ("accessibility_tree", "text", "action", "thought", "url"):
        _append_trajectory_value(lines, key, item.get(key))
    _append_trajectory_value(lines, "thought", item.get("thoughts"))
    observation = item.get("observation")
    if isinstance(observation, dict):
        for key in ("accessibility_tree", "text", "url"):
            _append_trajectory_value(lines, f"observation.{key}", observation.get(key))
    else:
        _append_trajectory_value(lines, "observation", observation)
    content = item.get("content")
    if isinstance(content, dict):
        _append_trajectory_item(lines, content)
    else:
        _append_trajectory_value(lines, "content", content)


def _append_trajectory_value(lines: list[str], label: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        lines.append(f"{label}: {value.strip()}")
    elif isinstance(value, list):
        for part in value:
            _append_trajectory_value(lines, label, part)
