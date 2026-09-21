"""Common memory-campaign adapters with explicit capability boundaries.

The campaign runner can compare local Engraphis with optional Mem0 OSS and
Graphiti installations without importing either competitor on the offline path.
Competitor constructors require an injected, budgeted LLM client or an explicit
client factory; their SDK defaults are never selected implicitly.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import importlib
import inspect
import json
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence


PINS_PATH = Path(__file__).resolve().parent / "configs" / "competitor-pins.json"


class AdapterError(RuntimeError):
    """A safe adapter error without provider payload text."""


class AdapterDependencyError(AdapterError):
    """An optional competitor dependency is unavailable."""


class AdapterCapabilityError(AdapterError):
    """A backend cannot represent the requested scope or temporal condition."""


class AdapterConfigurationError(AdapterError):
    """A backend would otherwise use an unbudgeted or ambiguous configuration."""


class BudgetedLLM(Protocol):
    is_budgeted: bool

    def complete(self, **kwargs: Any) -> Any:
        ...


_ROUTE_LOCK = threading.RLock()
_MEM0_ROUTES: dict[str, BudgetedLLM] = {}
_MEM0_PREVIOUS_OPENAI: dict[str, Any] = {}
_GRAPHITI_ROUTES: dict[str, BudgetedLLM] = {}
_ENGRAPHIS_CLOCK_LOCK = threading.RLock()


def _route_key(prefix: str, owner: Any) -> str:
    raw = f"{prefix}:{id(owner)}:{time.monotonic_ns()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _route_call_id(prefix: str, route: str, payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:48]
    return f"{prefix}-{route[:20]}-{digest}"


def _finite_timestamp(value: Any, *, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite timestamp")
    return result


@contextmanager
def _engraphis_clock(now: Optional[float]):
    """Inject an evaluation clock into the v2 modules for one operation.

    The production engine deliberately owns its wall clock and its public write API
    does not accept ``ingested_at``.  Campaign fixture mode therefore patches only the
    already-imported ``now_ts`` aliases while an isolated ingest/recall operation is
    running.  The default adapter path never enters this context, and every caller
    receives an explicit provenance marker when it does.
    """
    if now is None:
        yield
        return
    modules: list[Any] = []
    with _ENGRAPHIS_CLOCK_LOCK:
        for module_name in (
            "engraphis.core.engine",
            "engraphis.core.store",
            "engraphis.core.recall",
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            if hasattr(module, "now_ts"):
                modules.append((module, getattr(module, "now_ts")))
                setattr(module, "now_ts", lambda value=float(now): value)
        try:
            yield
        finally:
            for module, original in modules:
                setattr(module, "now_ts", original)


def _nonnegative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, *, name: str) -> int:
    result = _nonnegative_int(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


@dataclass(frozen=True)
class CampaignRecord:
    """Normalized chronological input record accepted by every adapter."""

    record_id: str
    content: str
    role: str = "user"
    timestamp: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    valid_to: Optional[float] = None
    title: str = ""
    subject_key: str = ""
    claim_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    operation: str = "remember"
    scope: str = "workspace"
    workspace: str = ""
    repo: str = ""
    session: str = ""
    trusted: bool = True
    corrects: Optional[str] = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, ordinal: int = 0) -> "CampaignRecord":
        if not isinstance(value, Mapping):
            raise ValueError("campaign records must be mappings")
        operation = str(value.get("op", value.get("operation", "remember")) or "remember").strip().casefold()
        if operation not in {"remember", "correct", "invalidate", "event"}:
            raise ValueError(f"unsupported campaign operation: {operation}")
        raw_id = value.get(
            "record_id", value.get("evidence_id", value.get("id", value.get("source_id")))
        )
        record_id = str(raw_id or f"record-{ordinal}").strip()
        content = value.get("content", value.get("text", value.get("message")))
        if not record_id or len(record_id) > 512:
            raise ValueError("campaign record id must be bounded and non-empty")
        if not isinstance(content, str) or (operation != "invalidate" and not content.strip()):
            raise ValueError("campaign record content must be non-empty text")
        timestamp = value.get("timestamp", value.get("created_at"))
        valid_at = value.get("valid_at", value.get("valid_from", timestamp))
        known_at = value.get("known_at", value.get("ingested_at"))
        valid_to = value.get("valid_to")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("campaign record metadata must be an object")
        scope = str(value.get("scope", "workspace") or "workspace").strip().casefold()
        if scope not in {"workspace", "repo", "session"}:
            raise ValueError("campaign record scope must be workspace, repo, or session")
        corrects = value.get("corrects")
        metadata_out = dict(metadata)
        metadata_out.setdefault("operation", operation)
        metadata_out.setdefault("scope", scope)
        metadata_out.setdefault("trusted", bool(value.get("trusted", True)))
        if corrects is not None:
            metadata_out.setdefault("corrects", str(corrects))
        return cls(
            record_id=record_id,
            content=content or "",
            role=str(value.get("role", "user") or "user"),
            timestamp=_finite_timestamp(timestamp, name="timestamp"),
            valid_at=_finite_timestamp(valid_at, name="valid_at"),
            known_at=_finite_timestamp(known_at, name="known_at"),
            valid_to=_finite_timestamp(valid_to, name="valid_to"),
            title=str(value.get("title", "") or ""),
            subject_key=str(value.get("subject_key", "") or ""),
            claim_kind=str(value.get("claim_kind", "") or ""),
            metadata=metadata_out,
            operation=operation,
            scope=scope,
            workspace=str(value.get("workspace", value.get("workspace_id", "")) or ""),
            repo=str(value.get("repo", value.get("repo_id", "")) or ""),
            session=str(value.get("session", value.get("session_id", "")) or ""),
            trusted=bool(value.get("trusted", True)),
            corrects=str(corrects) if corrects is not None else None,
        )


@dataclass(frozen=True)
class AdapterCapabilities:
    adapter: str
    version: str
    source: str
    source_revision: str
    scopes: tuple[str, ...]
    supports_valid_at: bool
    supports_known_at: bool
    supports_history: bool
    supports_graph: bool
    llm_requires_budgeted_client: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "version": self.version,
            "source": self.source,
            "source_revision": self.source_revision,
            "scopes": list(self.scopes),
            "supports_valid_at": self.supports_valid_at,
            "supports_known_at": self.supports_known_at,
            "supports_history": self.supports_history,
            "supports_graph": self.supports_graph,
            "llm_requires_budgeted_client": self.llm_requires_budgeted_client,
        }


@dataclass(frozen=True)
class AdapterUsage:
    token_budget: int
    context_tokens: int
    source_tokens: int
    packed_count: int
    omitted_count: int
    latency_ms: float
    token_counter: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_budget": self.token_budget,
            "context_tokens": self.context_tokens,
            "source_tokens": self.source_tokens,
            "packed_count": self.packed_count,
            "omitted_count": self.omitted_count,
            "latency_ms": round(self.latency_ms, 3),
            "token_counter": self.token_counter,
        }


@dataclass(frozen=True)
class AdapterRecall:
    context: str
    source_ids: tuple[str, ...]
    usage: AdapterUsage
    provenance: dict[str, Any]


_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_TOKEN_COUNTER_ID = "engraphis.regex.v1"


def _token_estimate(value: str) -> int:
    return len(_TOKEN_RE.findall(str(value or "")))


def _await(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise AdapterError("async competitor operation cannot run inside an active event loop")


def _call_with_fallbacks(
    method: Callable[..., Any],
    calls: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
    *,
    await_result: Callable[[Any], Any] = _await,
) -> Any:
    """Try only call shapes rejected by Python's signature binder.

    A ``TypeError`` raised after a method starts executing can represent a real
    provider/backend failure.  Retrying that exception would issue another paid
    write or duplicate a partially-applied write.  Signature inspection lets us
    skip incompatible shapes before execution and makes an in-body ``TypeError``
    terminal for that operation.
    """
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None

    last_error: Optional[Exception] = None
    attempted = False
    for args, kwargs in calls:
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        attempted = True
        try:
            return await_result(method(*args, **kwargs))
        except TypeError as exc:
            if signature is None:
                raise AdapterError(
                    "competitor SDK method raised TypeError after execution"
                ) from exc
            raise AdapterError(
                "competitor SDK method raised TypeError after execution"
            ) from exc
    if last_error is not None and not attempted:
        raise AdapterError("competitor SDK method signature is unsupported") from last_error
    raise AdapterError("competitor SDK method is unavailable")


def _result_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("results", "memories", "facts", "edges", "nodes", "items"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                return list(nested)
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _value(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        candidate = getattr(item, name, None)
        if candidate is not None:
            return candidate
    return default


def _source_id(item: Any, ordinal: int) -> str:
    value = _value(item, "source_id", "memory_id", "uuid", "id", default=None)
    if value is None:
        metadata = _value(item, "metadata", default={})
        if isinstance(metadata, Mapping):
            value = metadata.get("source_id", metadata.get("record_id"))
    return str(value or f"result-{ordinal}")


def _result_text(item: Any) -> str:
    return str(_value(
        item, "content", "memory", "text", "fact", "summary", "name", "description",
        default="",
    ) or "")


def _campaign_source_id(
    item: Any,
    ordinal: int,
    memory_ids: Mapping[str, str],
) -> Optional[str]:
    """Map a backend result to the fixture evidence id, never to a UUID gold key."""
    metadata = _value(item, "metadata", default={})
    if isinstance(metadata, Mapping):
        source = metadata.get("campaign_record_id", metadata.get("record_id"))
        if source:
            return str(source)
    backend_id = _source_id(item, ordinal)
    for record_id, candidate in memory_ids.items():
        if candidate == backend_id:
            return record_id
    # Graphiti search returns EntityEdge objects whose ``episodes`` field
    # contains the originating episode UUIDs, while the edge UUID itself is a
    # derived graph fact.  Resolve those episode references before admitting a
    # citation; a graph UUID is not a fixture evidence ID.
    episodes = _value(item, "episodes", default=())
    if isinstance(episodes, Sequence) and not isinstance(episodes, (str, bytes, bytearray)):
        for episode_id in episodes:
            for record_id, candidate in memory_ids.items():
                if candidate == str(episode_id):
                    return record_id
    # Graph edges and Mem0 projections may omit the original record id.  Keep the
    # context for qualitative inspection, but omit an ungrounded citation key.
    return None


def _campaign_trust(
    item: Any,
    source_id: str,
    trust_by_id: Optional[Mapping[str, bool]],
) -> Optional[bool]:
    """Resolve the campaign trust label without trusting backend display text."""

    metadata = _value(item, "metadata", default={})
    raw: Any = None
    if isinstance(metadata, Mapping):
        raw = metadata.get("campaign_trusted", metadata.get("trusted"))
        provenance = metadata.get("provenance")
        if raw is None and isinstance(provenance, Mapping):
            raw = provenance.get("trusted")
    if raw is None and trust_by_id is not None:
        raw = trust_by_id.get(source_id)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().casefold()
        if normalized in {"true", "1", "yes", "trusted"}:
            return True
        if normalized in {"false", "0", "no", "untrusted"}:
            return False
        return None
    if raw is None:
        return None
    return bool(raw)


def _merge_peer_result_pages(pages: Sequence[Sequence[Any]]) -> list[Any]:
    """Merge partition pages before the common evidence ``k`` limit.

    Mem0 returns an independently ranked page for each physical partition.  A
    flat append makes the first partition consume the entire common limit.  Use
    comparable backend scores when present; otherwise interleave pages so no
    selected partition is silently starved by page order.
    """

    normalized_pages = [list(page) for page in pages if page]
    rows = [
        (
            item,
            _value(item, "score", "similarity", "relevance", default=None),
            page_index,
            rank,
        )
        for page_index, page in enumerate(normalized_pages)
        for rank, item in enumerate(page)
    ]
    if not rows:
        return []
    scored: list[tuple[Any, float, int, int]] = []
    for item, raw_score, page_index, rank in rows:
        try:
            score = float(raw_score)
        except (TypeError, ValueError, OverflowError):
            scored = []
            break
        if not math.isfinite(score):
            scored = []
            break
        scored.append((item, score, page_index, rank))
    if scored and len(scored) == len(rows):
        scored.sort(key=lambda row: (-row[1], row[2], row[3]))
        return [row[0] for row in scored]

    merged: list[Any] = []
    for rank in range(max(len(page) for page in normalized_pages)):
        for page in normalized_pages:
            if rank < len(page):
                merged.append(page[rank])
    return merged


def _pack_peer_items(
    items: Sequence[Any],
    *,
    query: str,
    k: int,
    token_budget: int,
    memory_ids: Mapping[str, str],
    trust_by_id: Optional[Mapping[str, bool]] = None,
) -> tuple[str, tuple[str, ...], AdapterUsage, int]:
    """Pack complete peer facts under the same deterministic counter as Engraphis."""
    del query  # Peer APIs already rank; packing must not re-rank their evidence.
    limit = max(0, token_budget)
    selected_text: list[str] = []
    selected_ids: list[str] = []
    source_tokens = 0
    omitted = 0
    unmapped = 0
    mapped_candidates = 0
    for index, item in enumerate(items):
        if mapped_candidates >= max(0, k):
            break
        text = _result_text(item).strip()
        if not text:
            omitted += 1
            continue
        source_tokens += _token_estimate(text)
        source_id = _campaign_source_id(item, index, memory_ids)
        if source_id is None:
            # Keep each admitted context unit positionally attributable to one
            # fixture record.  An unmapped backend projection is qualitative
            # evidence only and must not shift the IDs used for citation scoring.
            unmapped += 1
            omitted += 1
            continue
        mapped_candidates += 1
        trusted = _campaign_trust(item, source_id, trust_by_id)
        trust_label = "unknown" if trusted is None else str(trusted).lower()
        rendered = f"[{source_id}] trusted={trust_label}\n{text}"
        tokens = _token_estimate(rendered)
        if tokens > limit:
            omitted += 1
            continue
        selected_text.append(rendered)
        limit -= tokens
        selected_ids.append(source_id)
    context = "\n\n".join(selected_text)
    usage = AdapterUsage(
        token_budget=token_budget,
        context_tokens=_token_estimate(context),
        source_tokens=source_tokens,
        packed_count=len(selected_text),
        omitted_count=omitted,
        latency_ms=0.0,
        token_counter=_TOKEN_COUNTER_ID,
    )
    return context, tuple(selected_ids), usage, unmapped


def _response_schema(response_format: Any) -> Optional[dict[str, Any]]:
    if not isinstance(response_format, Mapping):
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return {"format": {"type": "json_object"}}
    if kind != "json_schema":
        return None
    schema = response_format.get("json_schema", response_format)
    if not isinstance(schema, Mapping):
        return None
    return {"format": {
        "type": "json_schema",
        "name": str(schema.get("name", "mem0_output")),
        "schema": schema.get("schema", {}),
        "strict": bool(schema.get("strict", True)),
    }}


def _register_mem0_budget_route(route: str, client: BudgetedLLM) -> None:
    """Register one explicit Mem0 LLM provider without importing Mem0 offline."""
    try:
        factory_module = importlib.import_module("mem0.utils.factory")
        base_module = importlib.import_module("mem0.configs.llms.base")
        llm_module = importlib.import_module("mem0.llms.base")
        factory = getattr(factory_module, "LlmFactory")
        base_config = getattr(base_module, "BaseLlmConfig")
        llm_base = getattr(llm_module, "LLMBase")
    except (ImportError, AttributeError) as exc:  # pragma: no cover - optional dependency
        raise AdapterDependencyError("mem0ai==2.0.20 is not installed") from exc

    config_name = "_EngraphisMem0RouteConfig"
    llm_name = "_EngraphisMem0RouteLLM"
    config_cls = globals().get(config_name)
    if config_cls is None:
        def _config_init(self: Any, *args: Any, route_id: str = "", **kwargs: Any) -> None:
            # Mem0's BaseLlmConfig is a plain class (not a Pydantic model), so
            # an annotation alone does not make ``route_id`` an accepted
            # constructor parameter for LlmFactory.create().
            base_config.__init__(self, *args, **kwargs)
            self.route_id = route_id

        config_cls = type(
            config_name,
            (base_config,),
            {"__init__": _config_init},
        )
        globals()[config_name] = config_cls

    if globals().get(llm_name) is None:
        def _init(self: Any, config: Any = None) -> None:
            llm_base.__init__(self, config)
            route_id = str(getattr(self.config, "route_id", ""))
            with _ROUTE_LOCK:
                if route_id not in _MEM0_ROUTES:
                    raise AdapterConfigurationError("Mem0 route is no longer active")
            self._route_id = route_id

        def _generate(
            self: Any,
            messages: list[Mapping[str, Any]],
            response_format: Any = None,
            tools: Optional[list[dict[str, Any]]] = None,
            tool_choice: str = "auto",
            **kwargs: Any,
        ) -> Any:
            with _ROUTE_LOCK:
                route_client = _MEM0_ROUTES.get(self._route_id)
            if route_client is None:
                raise AdapterConfigurationError("Mem0 route is no longer active")
            payload = {
                "messages": [dict(message) for message in messages],
                "tools": tools or [],
                "tool_choice": tool_choice,
            }
            max_tokens = int(getattr(self.config, "max_tokens", 2048) or 2048)
            result = route_client.complete(
                call_id=_route_call_id("mem0", self._route_id, payload),
                kind="ingest",
                input=payload,
                max_output_tokens=max(1, max_tokens),
                text=_response_schema(response_format),
            )
            raw = str(getattr(result, "text", result))
            # Mem0's ``Memory._add_to_vector_store`` parses the provider result
            # as a JSON string after ``generate_response`` returns.  Returning a
            # decoded dict here would make the SDK call ``.strip()`` on a dict
            # and silently turn a successful extraction into an empty result.
            return raw

        globals()[llm_name] = type(
            llm_name,
            (llm_base,),
            {"__init__": _init, "generate_response": _generate},
        )
    class_path = f"eval.campaign_adapters.{llm_name}"
    register = getattr(factory, "register_provider", None)
    if not callable(register):
        raise AdapterDependencyError("Mem0 LlmFactory lacks provider registration")
    # Mem0 2.0.20 validates the provider name against a closed list in
    # ``mem0.llms.configs.LlmConfig`` before consulting ``LlmFactory``.  A custom
    # provider therefore cannot survive ``Memory.from_config``.  Rebind the
    # accepted ``openai`` slot to our route class, then restore it when the adapter
    # closes; no OpenAI SDK is constructed and every generation still reaches the
    # budget client above.  Campaign attempts are serialized at the adapter layer.
    with _ROUTE_LOCK:
        previous = factory.provider_to_class.get("openai")
        _MEM0_PREVIOUS_OPENAI[route] = previous
        factory.provider_to_class["openai"] = (class_path, config_cls)


def _restore_mem0_budget_route(route: str) -> None:
    """Restore Mem0's original OpenAI provider mapping after adapter shutdown."""
    with _ROUTE_LOCK:
        previous = _MEM0_PREVIOUS_OPENAI.pop(route, None)
        if previous is not None:
            try:
                factory_module = importlib.import_module("mem0.utils.factory")
                factory = getattr(factory_module, "LlmFactory")
                current = factory.provider_to_class.get("openai")
                class_path = "eval.campaign_adapters._EngraphisMem0RouteLLM"
                if current is not None and current[0] == class_path:
                    factory.provider_to_class["openai"] = previous
            except (ImportError, AttributeError):
                # The optional dependency may have been unloaded during process
                # teardown; the route registry is still cleared below.
                pass


def _register_graphiti_budget_route(route: str, client: BudgetedLLM) -> Any:
    """Create a Graphiti LLMClient whose only dispatch is CampaignAPI.complete."""
    try:
        client_module = importlib.import_module("graphiti_core.llm_client.client")
        config_module = importlib.import_module("graphiti_core.llm_client.config")
        llm_base = getattr(client_module, "LLMClient")
        llm_config = getattr(config_module, "LLMConfig")
    except (ImportError, AttributeError) as exc:  # pragma: no cover - optional dependency
        raise AdapterDependencyError("graphiti-core==0.29.3 is not installed") from exc

    with _ROUTE_LOCK:
        _GRAPHITI_ROUTES[route] = client

    class RoutedGraphitiLLM(llm_base):
        def __init__(self) -> None:
            super().__init__(
                config=llm_config(model="gpt-5.6-luna", small_model="gpt-5.6-luna"),
                cache=False,
            )
            self._route_id = route

        async def _generate_response(
            self,
            messages: list[Any],
            response_model: Any = None,
            max_tokens: int = 4096,
            model_size: Any = None,
        ) -> dict[str, Any]:
            # Graphiti 0.29.3 keeps this abstract hook even though campaign
            # dispatch overrides ``generate_response`` below to avoid the SDK's
            # built-in retry decorator (the ledger owns retry/resume policy).
            return await self.generate_response(
                messages,
                response_model=response_model,
                max_tokens=max_tokens,
                model_size=model_size,
            )

        async def generate_response(
            self,
            messages: list[Any],
            response_model: Any = None,
            max_tokens: Optional[int] = None,
            model_size: Any = None,
            group_id: Optional[str] = None,
            prompt_name: Optional[str] = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            with _ROUTE_LOCK:
                route_client = _GRAPHITI_ROUTES.get(self._route_id)
            if route_client is None:
                raise AdapterConfigurationError("Graphiti route is no longer active")
            payload_messages = [
                {
                    "role": str(getattr(message, "role", "user")),
                    "content": str(getattr(message, "content", message)),
                }
                for message in messages
            ]
            schema: Optional[dict[str, Any]] = None
            if response_model is not None:
                schema_method = getattr(response_model, "model_json_schema", None)
                if not callable(schema_method):
                    schema_method = getattr(response_model, "schema", None)
                schema = {
                    "type": "json_schema",
                    "name": getattr(response_model, "__name__", "graphiti_output"),
                    "schema": schema_method() if callable(schema_method) else {},
                    "strict": True,
                }
            payload = {
                "messages": payload_messages,
                "group_id": group_id,
                "prompt_name": prompt_name,
                "schema": schema,
            }
            result = route_client.complete(
                call_id=_route_call_id("graphiti", self._route_id, payload),
                kind="ingest",
                input=payload_messages,
                max_output_tokens=max(1, int(max_tokens or 4096)),
                text={"format": schema} if schema is not None else None,
            )
            raw = str(getattr(result, "text", result))
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return {"content": raw}
            return parsed if isinstance(parsed, dict) else {"content": raw}

    routed = RoutedGraphitiLLM()
    return routed


def build_graphiti_local_components(
    *,
    embed_model: str,
    embed_revision: Optional[str] = None,
    cross_encoder_model: Optional[str] = None,
    cross_encoder_revision: Optional[str] = None,
) -> tuple[Any, Any]:
    """Build explicit local Graphiti embedder and cross-encoder components.

    Graphiti 0.29.3 ships provider clients whose defaults are networked OpenAI
    services.  This helper keeps that choice at the campaign composition
    boundary: ``sentence-transformers`` is imported only when a peer attempt
    explicitly asks for local components, and the returned objects satisfy the
    pinned Graphiti protocols without adding a core dependency.
    """
    try:
        from graphiti_core.cross_encoder.client import CrossEncoderClient
        from graphiti_core.embedder.client import EmbedderClient
    except (ImportError, AttributeError) as exc:  # pragma: no cover - optional dependency
        raise AdapterDependencyError("graphiti-core==0.29.3 is not installed") from exc
    try:
        from sentence_transformers import CrossEncoder, SentenceTransformer
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise AdapterDependencyError(
            "sentence-transformers is required for local Graphiti components"
        ) from exc

    class LocalEmbedder(EmbedderClient):
        def __init__(self) -> None:
            self._model: Any = None

        def _load(self) -> Any:
            if self._model is None:
                kwargs = {}
                if embed_revision:
                    kwargs["revision"] = embed_revision
                self._model = SentenceTransformer(embed_model, **kwargs)
            return self._model

        async def create(self, input_data: Any) -> list[float]:
            if not isinstance(input_data, str):
                values = list(input_data)
                if values and not isinstance(values[0], str):
                    # Graphiti's single-item protocol may receive a prebuilt
                    # numeric vector; preserve it without a second encoding.
                    return [float(value) for value in values]
                sentences = [str(value) for value in values]
            else:
                sentences = [input_data]
            model = self._load()
            vector = model.encode(
                sentences,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            # Graphiti's ``create`` contract returns one vector even though it
            # passes a one-item list to this method.
            first = vector[0] if getattr(vector, "ndim", 1) > 1 else vector
            return [float(value) for value in first]

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            model = self._load()
            vectors = model.encode(
                input_data_list,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            return [[float(value) for value in vector] for vector in vectors]

    class LocalCrossEncoder(CrossEncoderClient):
        def __init__(self) -> None:
            self._model: Any = None

        def _load(self) -> Any:
            if self._model is None:
                if not cross_encoder_model:
                    return None
                kwargs = {}
                if cross_encoder_revision:
                    kwargs["revision"] = cross_encoder_revision
                self._model = CrossEncoder(cross_encoder_model, **kwargs)
            return self._model

        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            model = self._load()
            if model is None:
                query_terms = set(re.findall(r"\w+", query.casefold()))
                scored = [
                    (
                        passage,
                        float(
                            len(query_terms & set(re.findall(r"\w+", passage.casefold())))
                            / max(1, len(query_terms))
                        ),
                    )
                    for passage in passages
                ]
            else:
                values = model.predict([(query, passage) for passage in passages])
                scored = [(passage, float(score)) for passage, score in zip(passages, values)]
            return sorted(scored, key=lambda item: (-item[1], item[0]))

    return LocalEmbedder(), LocalCrossEncoder()


def build_mem0_local_config(
    *,
    embed_model: str,
    embed_revision: Optional[str],
    vector_path: str | Path,
    collection_name: str,
    history_db_path: str | Path,
    embedding_model_dims: int = 384,
) -> dict[str, Any]:
    """Return an explicit Mem0 OSS local embedder/Qdrant configuration.

    The LLM section is intentionally omitted: ``Mem0Adapter`` inserts the
    budgeted provider route after validating this local storage configuration.
    Paths and collection names are caller-owned so each attempt can use a
    disposable namespace and cannot read another attempt's vectors/history.
    """
    if not isinstance(embed_model, str) or not embed_model.strip():
        raise ValueError("embed_model must be non-empty")
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise ValueError("collection_name must be non-empty")
    if isinstance(embedding_model_dims, bool) or not isinstance(embedding_model_dims, int):
        raise ValueError("embedding_model_dims must be a positive integer")
    if embedding_model_dims <= 0:
        raise ValueError("embedding_model_dims must be a positive integer")
    embed_config: dict[str, Any] = {"model": embed_model}
    if embed_revision:
        embed_config["model_kwargs"] = {"revision": str(embed_revision)}
    return {
        "embedder": {"provider": "huggingface", "config": embed_config},
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(Path(vector_path)),
                "collection_name": collection_name,
                "embedding_model_dims": embedding_model_dims,
            },
        },
        "history_db_path": str(Path(history_db_path)),
    }


class CampaignAdapter(Protocol):
    capabilities: AdapterCapabilities

    def prepare(self, *, workspace_id: str, repo_id: Optional[str] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
        ...

    def reset(self) -> None:
        ...

    def ingest(self, records: Iterable[Mapping[str, Any]]) -> list[str]:
        ...

    def recall(self, query: str, *, k: int = 10, token_budget: int = 1024,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None) -> AdapterRecall:
        ...

    def metrics(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class _BaseAdapter:
    capabilities: AdapterCapabilities

    def __init__(self) -> None:
        self.workspace_id: Optional[str] = None
        self.repo_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._prepared = False
        self._memory_ids: dict[str, str] = {}
        self._counters: Counter[str] = Counter()

    def prepare(self, *, workspace_id: str, repo_id: Optional[str] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("workspace_id must be non-empty")
        self.workspace_id = workspace_id
        self.repo_id = repo_id
        self.session_id = session_id
        self._prepared = True
        self._counters["prepare"] += 1
        return {
            "adapter": self.capabilities.adapter,
            "version": self.capabilities.version,
            "workspace_id": workspace_id,
            "repo_id": repo_id,
            "session_id": session_id,
            "capabilities": self.capabilities.as_dict(),
        }

    def _ensure_prepared(self) -> None:
        if not self._prepared or self.workspace_id is None:
            raise AdapterError("adapter.prepare() must run before ingest or recall")

    def _check_filters(self, *, valid_at: Optional[float], known_at: Optional[float]) -> None:
        if self.repo_id is not None and "repo" not in self.capabilities.scopes:
            raise AdapterCapabilityError(f"{self.capabilities.adapter} cannot represent repo scope")
        if self.session_id is not None and "session" not in self.capabilities.scopes:
            raise AdapterCapabilityError(f"{self.capabilities.adapter} cannot represent session scope")
        if valid_at is not None and not self.capabilities.supports_valid_at:
            raise AdapterCapabilityError(f"{self.capabilities.adapter} cannot filter valid_at")
        if known_at is not None and not self.capabilities.supports_known_at:
            raise AdapterCapabilityError(f"{self.capabilities.adapter} cannot filter known_at")

    def _normalize_records(
        self, records: Iterable[Mapping[str, Any]],
    ) -> list[CampaignRecord]:
        normalized = [
            CampaignRecord.from_mapping(item, ordinal=index)
            for index, item in enumerate(records)
        ]
        normalized.sort(
            key=lambda item: (
                item.timestamp is None,
                item.timestamp if item.timestamp is not None else 0.0,
                # Python's stable sort preserves fixture ordinal for ties and
                # records with no timestamp.
            )
        )
        return normalized

    def _check_record_scope(self, record: CampaignRecord) -> None:
        """Reject scope claims a backend cannot represent instead of flattening them."""
        if record.scope not in self.capabilities.scopes:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} cannot represent {record.scope} scope"
            )
        if record.operation in {"correct", "invalidate"} and not self.capabilities.supports_history:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} cannot represent {record.operation} history"
            )

    def _record_workspace(self, record: CampaignRecord) -> str:
        return record.workspace or str(self.workspace_id or "")

    def _record_repo(self, record: CampaignRecord) -> Optional[str]:
        return record.repo or self.repo_id

    def _record_session(self, record: CampaignRecord) -> Optional[str]:
        return record.session or self.session_id

    def metrics(self) -> dict[str, Any]:
        return {
            "adapter": self.capabilities.adapter,
            "capabilities": self.capabilities.as_dict(),
            "counters": dict(self._counters),
            "ingested_records": len(self._memory_ids),
        }

    def reset(self) -> None:
        self._memory_ids.clear()
        self._counters.clear()
        self._prepared = False

    def close(self) -> None:
        self._counters["close"] += 1


class _PeerAdapter(_BaseAdapter):
    """Shared workspace isolation and preflight for optional peer stores.

    Mem0 and Graphiti expose a single user/group partition rather than the
    Engraphis workspace/repo/session hierarchy.  A namespace keeps separate
    benchmark attempts disjoint, while the logical workspace label remains in
    each record's provenance.  Records are fully checked before a client is
    touched so an unsupported repo/session/history row cannot consume an LLM
    extraction call before the adapter reports ``unsupported``.
    """

    def __init__(self, *, config: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._namespace = str(self._config.get("namespace", "") or "").strip()
        self._scope_partition = str(
            self._config.get("scope_partition", "workspace") or "workspace"
        ).strip().casefold()
        if self._scope_partition not in {"workspace", "repo"}:
            raise AdapterConfigurationError(
                "peer scope_partition must be workspace or repo"
            )
        self._workspace_label = ""
        self._repo_label = ""
        self._workspace_partition = ""
        self._repo_partition = ""
        self._selected_partitions: tuple[str, ...] = ()
        self._record_partitions: dict[str, str] = {}
        self._backend_partitions: dict[str, str] = {}
        self._record_trust: dict[str, bool] = {}

    @staticmethod
    def _partition_id(namespace: str, workspace: str) -> str:
        logical = str(workspace).strip()
        if not logical:
            raise ValueError("workspace_id must be non-empty")
        if not namespace and re.fullmatch(r"[A-Za-z0-9_-]+", logical):
            return logical
        # Graphiti validates group IDs as ASCII alphanumeric, dash, underscore.
        # A digest avoids collisions from punctuation normalization and keeps
        # the physical partition bounded without exposing fixture text.
        digest = hashlib.sha256(
            f"{namespace or 'default'}\0{logical}".encode("utf-8", "surrogatepass")
        ).hexdigest()[:32]
        return f"campaign_{digest}"

    def prepare(self, *, workspace_id: str, repo_id: Optional[str] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
        if session_id:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} exposes no session partition; "
                "prepare without session_id"
            )
        if self._scope_partition == "workspace" and repo_id:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} is configured for workspace partitioning; "
                "set scope_partition='repo' for an isolated repo attempt"
            )
        if self._scope_partition == "repo" and not repo_id:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} repo partition requires repo_id"
            )
        self._workspace_label = str(workspace_id)
        self._repo_label = str(repo_id or "")
        self._workspace_partition = self._partition_id(
            self._namespace, self._workspace_label,
        )
        self._repo_partition = (
            self._partition_id(
                self._namespace,
                f"{self._workspace_label}\0{self._repo_label}",
            )
            if self._repo_label else ""
        )
        # Repo attempts physically isolate every logical repo, while the selected
        # read includes the workspace ancestor partition plus the requested repo.
        # Sibling repos are admitted into their own partition so a corpus containing
        # them can be ingested without leaking them into the selected recall.
        self._selected_partitions = (
            (self._workspace_partition,)
            if self._scope_partition == "workspace"
            else (self._workspace_partition, self._repo_partition)
        )
        prepared = super().prepare(
            workspace_id=self._workspace_partition, repo_id=None, session_id=None,
        )
        prepared["workspace_label"] = self._workspace_label
        prepared["repo_label"] = self._repo_label or None
        prepared["scope_partition"] = self._scope_partition
        prepared["scope_projection"] = (
            "isolated_workspace_partition"
            if self._scope_partition == "workspace"
            else "isolated_repo_partition_without_session_history"
        )
        prepared["namespace"] = self._namespace or None
        return prepared

    def _record_workspace(self, record: CampaignRecord) -> str:
        requested = str(record.workspace or "").strip()
        if requested and requested not in {self._workspace_label, self.workspace_id}:
            raise AdapterCapabilityError(
                f"{self.capabilities.adapter} record crosses the prepared workspace boundary"
            )
        if record.scope == "repo":
            repo = str(record.repo or "").strip()
            if not repo:
                raise AdapterCapabilityError(
                    f"{self.capabilities.adapter} repo records require repo"
                )
            return self._partition_id(
                self._namespace, f"{self._workspace_label}\0{repo}",
            )
        return str(self._workspace_partition or self.workspace_id or "")

    def _record_partition(self, record: CampaignRecord) -> str:
        return self._record_workspace(record)

    def _allowed_partitions(self) -> set[str]:
        return set(self._selected_partitions)

    def _filter_partition_items(self, items: Sequence[Any]) -> list[Any]:
        """Admit only results whose ingest partition belongs to this read."""
        allowed = self._allowed_partitions()
        admitted: list[Any] = []
        seen: set[str] = set()
        for item in items:
            backend_id = _source_id(item, len(admitted))
            partition = self._backend_partitions.get(backend_id)
            if partition is None:
                metadata = _value(item, "metadata", default={})
                if isinstance(metadata, Mapping):
                    partition = str(metadata.get("campaign_partition", "") or "")
                    record_id = str(
                        metadata.get("campaign_record_id", metadata.get("record_id", ""))
                        or ""
                    )
                    if not partition and record_id:
                        partition = self._record_partitions.get(record_id)
                episodes = _value(item, "episodes", default=())
                if not partition and isinstance(episodes, Sequence) and not isinstance(
                    episodes, (str, bytes, bytearray)
                ):
                    for episode_id in episodes:
                        candidate = self._backend_partitions.get(str(episode_id))
                        if candidate:
                            partition = candidate
                            break
            # Unknown backend results cannot be safely attributed to a selected
            # campaign partition, so they remain qualitative omissions.
            if partition not in allowed:
                continue
            dedupe_key = _source_id(item, len(admitted))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            admitted.append(item)
        return admitted

    def _preflight_records(self, records: Sequence[CampaignRecord]) -> None:
        for record in records:
            if record.operation in {"correct", "invalidate"}:
                raise AdapterCapabilityError(
                    f"{self.capabilities.adapter} cannot represent {record.operation} history"
                )
            allowed_scopes = (
                {"workspace"}
                if self._scope_partition == "workspace"
                else {"workspace", "repo"}
            )
            if record.scope not in allowed_scopes:
                raise AdapterCapabilityError(
                    f"{self.capabilities.adapter} cannot represent {record.scope} scope"
                )
            if self._scope_partition == "repo" and record.scope == "repo":
                if not str(record.repo or "").strip():
                    raise AdapterCapabilityError(
                        f"{self.capabilities.adapter} repo record has no repo partition"
                    )
            # Validate workspace partition identity before the first add call.
            self._record_workspace(record)

    def reset(self) -> None:
        self._record_partitions.clear()
        self._backend_partitions.clear()
        self._record_trust.clear()
        super().reset()

    def metrics(self) -> dict[str, Any]:
        result = super().metrics()
        result.update({
            "scope_partition": self._scope_partition,
            "scope_projection": (
                "isolated_workspace_partition"
                if self._scope_partition == "workspace"
                else "isolated_repo_partition_without_session_history"
            ),
            "workspace_label": self._workspace_label,
            "repo_label": self._repo_label or None,
            "namespace": self._namespace or None,
        })
        return result


class EngraphisAdapter(_BaseAdapter):
    capabilities = AdapterCapabilities(
        adapter="engraphis",
        version="1.7.4",
        source="https://github.com/Coding-Dev-Tools/engraphis",
        source_revision="ca790261f499e0d124cdc7131235ffff89637248",
        scopes=("workspace", "repo", "session"),
        supports_valid_at=True,
        supports_known_at=True,
        supports_history=True,
        supports_graph=True,
        llm_requires_budgeted_client=False,
    )

    def __init__(
        self,
        *,
        engine: Any = None,
        db_path: str = ":memory:",
        engine_factory: Optional[Callable[..., Any]] = None,
        engine_kwargs: Optional[Mapping[str, Any]] = None,
        baseline_label: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._owned_engine = engine is None
        self._db_path = db_path
        self._engine_factory = engine_factory
        self._engine_kwargs = dict(engine_kwargs or {})
        # Fixture clocks are an explicit eval-only opt-in.  Keep their keys out of
        # the production factory kwargs: the v2 engine has no public clock parameter
        # and should retain its real wall-clock behavior outside this adapter.
        raw_clock = self._engine_kwargs.pop("fixture_clock", None)
        if raw_clock is None:
            clock_config: dict[str, Any] = {}
        elif isinstance(raw_clock, Mapping):
            clock_config = dict(raw_clock)
        else:
            raise AdapterConfigurationError("fixture_clock must be an object")
        if "fixture_clock_anchor" in self._engine_kwargs:
            clock_config.setdefault(
                "anchor", self._engine_kwargs.pop("fixture_clock_anchor"),
            )
        if "fixture_clock_origin" in self._engine_kwargs:
            clock_config.setdefault(
                "logical_origin", self._engine_kwargs.pop("fixture_clock_origin"),
            )
        enabled = bool(
            clock_config.get("enabled", False)
            or clock_config.get("mode") == "anchored"
            or raw_clock is not None
        )
        self._fixture_clock_enabled = enabled
        self._fixture_clock_anchor = _finite_timestamp(
            clock_config.get("anchor"), name="fixture_clock.anchor",
        )
        self._fixture_clock_origin = _finite_timestamp(
            clock_config.get("logical_origin", 0.0),
            name="fixture_clock.logical_origin",
        ) or 0.0
        if self._fixture_clock_enabled and self._fixture_clock_anchor is None:
            raise AdapterConfigurationError(
                "fixture_clock mode requires an explicit UNIX anchor"
            )
        self._fixture_clock_current: Optional[float] = None
        self._fixture_clock_max: Optional[float] = None
        self._workspace_label = ""
        self._repo_label = ""
        self._session_label = ""
        self._session_ids: dict[tuple[str, Optional[str], str], str] = {}
        self.baseline_label = str(baseline_label or "").strip().casefold() or None
        self._baseline_spec: Any = None
        self._arm_config: Any = None
        if self.baseline_label is not None:
            try:
                from eval.harness import executable_baseline
                self._baseline_spec = executable_baseline(self.baseline_label)
            except (ImportError, ValueError) as exc:
                raise AdapterConfigurationError(
                    f"Engraphis baseline is not executable: {baseline_label!r}"
                ) from exc
            if self._baseline_spec.mode != "retrieval":
                raise AdapterConfigurationError(
                    "campaign adapter supports retrieval baselines only"
                )
            self._arm_config = self._baseline_spec.arm_config

    def _map_fixture_time(self, value: Optional[float]) -> Optional[float]:
        if value is None or not self._fixture_clock_enabled:
            return value
        return float(self._fixture_clock_anchor) + (
            float(value) - self._fixture_clock_origin
        )

    def _fixture_now_for(self, record: CampaignRecord) -> Optional[float]:
        if not self._fixture_clock_enabled:
            return None
        candidates = [
            float(value) for value in (record.known_at, record.timestamp, record.valid_at)
            if value is not None
        ]
        logical = max(candidates) if candidates else None
        if logical is None:
            logical = self._fixture_clock_current
        if logical is None:
            logical = self._fixture_clock_origin
        self._fixture_clock_current = max(
            float(logical), float(self._fixture_clock_current or logical),
        )
        self._fixture_clock_max = max(
            float(logical), float(self._fixture_clock_max or logical),
        )
        return self._map_fixture_time(self._fixture_clock_current)

    def _run_clocked(self, record: CampaignRecord, callback: Callable[[], Any]) -> Any:
        with _engraphis_clock(self._fixture_now_for(record)):
            return callback()

    def _run_recall_clocked(self, callback: Callable[[], Any]) -> Any:
        if not self._fixture_clock_enabled:
            return callback()
        logical = self._fixture_clock_current
        if logical is None:
            logical = self._fixture_clock_origin
        self._fixture_clock_current = max(float(logical), float(self._fixture_clock_origin))
        return_value = self._map_fixture_time(self._fixture_clock_current)
        with _engraphis_clock(return_value):
            return callback()

    @property
    def engine(self) -> Any:
        if self._engine is None:
            try:
                from engraphis.factory import create_memory_engine
            except ImportError as exc:  # pragma: no cover - package-local failure
                raise AdapterDependencyError("Engraphis v2 factory is unavailable") from exc
            factory = self._engine_factory or create_memory_engine
            options = {
                "vector_backend": "numpy",
                "extractor": "none",
                "graph_extractor": "none",
                **self._engine_kwargs,
            }
            self._engine = factory(self._db_path, **options)
        return self._engine

    def prepare(self, *, workspace_id: str, repo_id: Optional[str] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
        """Resolve fixture names into durable workspace/repo/session ids."""
        self._workspace_label = str(workspace_id)
        self._repo_label = str(repo_id or "")
        self._session_label = str(session_id or "")
        engine = self.engine
        store = getattr(engine, "store", None)
        if store is not None and hasattr(store, "get_or_create_workspace"):
            try:
                workspace_id = self._resolve_workspace(store, workspace_id)
                repo_id = self._resolve_repo(store, workspace_id, repo_id)
                session_id = self._resolve_session(store, workspace_id, repo_id, session_id)
            except (OSError, RuntimeError, ValueError) as exc:
                raise AdapterConfigurationError("Engraphis scope initialization failed") from exc
        prepared = super().prepare(
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
        )
        prepared["baseline_label"] = self.baseline_label
        return prepared

    def _record_workspace(self, record: CampaignRecord) -> str:
        if not record.workspace or record.workspace in {
            self._workspace_label, self.workspace_id,
        }:
            return str(self.workspace_id or "")
        store = getattr(self.engine, "store", None)
        if store is not None and hasattr(store, "get_or_create_workspace"):
            return self._resolve_workspace(store, record.workspace)
        return record.workspace

    def _record_repo(self, record: CampaignRecord) -> Optional[str]:
        if not record.repo or record.repo in {self._repo_label, self.repo_id}:
            return self.repo_id
        store = getattr(self.engine, "store", None)
        if store is not None and self._record_workspace(record):
            return self._resolve_repo(store, self._record_workspace(record), record.repo)
        return record.repo

    def _record_session(self, record: CampaignRecord) -> Optional[str]:
        if not record.session:
            return self.session_id
        store = getattr(self.engine, "store", None)
        if store is not None:
            return self._resolve_session(
                store, self._record_workspace(record), self._record_repo(record), record.session,
            )
        return record.session

    @staticmethod
    def _resolve_workspace(store: Any, value: str) -> str:
        candidate = str(value).strip()
        if candidate.startswith("ws_"):
            row = store.conn.execute(
                "SELECT id FROM workspaces WHERE id=?", (candidate,)
            ).fetchone()
            if row is not None:
                return candidate
        return str(store.get_or_create_workspace(candidate))

    @staticmethod
    def _resolve_repo(store: Any, workspace_id: str, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        candidate = str(value).strip()
        if candidate.startswith("repo_"):
            row = store.conn.execute(
                "SELECT id FROM repos WHERE id=? AND workspace_id=?",
                (candidate, workspace_id),
            ).fetchone()
            if row is not None:
                return candidate
        return str(store.get_or_create_repo(workspace_id, candidate))

    def _resolve_session(
        self, store: Any, workspace_id: str, repo_id: Optional[str], value: Optional[str],
    ) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        candidate = str(value).strip()
        if candidate.startswith("ses_"):
            row = store.get_session(candidate)
            if row is not None:
                if row.get("workspace_id") != workspace_id or row.get("repo_id") != repo_id:
                    raise AdapterConfigurationError("Engraphis session belongs to a different scope")
                return candidate
        key = (workspace_id, repo_id, candidate)
        if key not in self._session_ids:
            self._session_ids[key] = str(store.start_session(workspace_id, repo_id, agent="campaign"))
        return self._session_ids[key]

    def ingest(self, records: Iterable[Mapping[str, Any]]) -> list[str]:
        self._ensure_prepared()
        from engraphis.core.interfaces import MemoryType, Scope

        normalized = self._normalize_records(records)
        result_ids: list[str] = []
        for record in normalized:
            self._check_record_scope(record)
            self._fixture_now_for(record)
            valid_at = self._map_fixture_time(record.valid_at)
            valid_to = self._map_fixture_time(record.valid_to)
            workspace_id = self._record_workspace(record)
            repo_id = self._record_repo(record)
            session_id = self._record_session(record)
            if record.scope == "workspace":
                repo_id = None
                session_id = None
            elif record.scope == "repo":
                session_id = None
            scope = Scope(record.scope)
            metadata = dict(record.metadata)
            provenance = {
                "source": "campaign",
                "trusted": record.trusted,
                "review_state": "approved" if record.trusted else "pending",
            }
            if self._fixture_clock_enabled:
                provenance.update({
                    "clock_mode": "fixture_anchor",
                    "fixture_clock_anchor": self._fixture_clock_anchor,
                    "fixture_logical_ingested_at": (
                        record.known_at
                        if record.known_at is not None
                        else record.timestamp
                        if record.timestamp is not None
                        else record.valid_at
                    ),
                })
            metadata.update({
                "campaign_record_id": record.record_id,
                "campaign_role": record.role,
                "campaign_operation": record.operation,
                "campaign_scope": record.scope,
                "campaign_workspace": workspace_id,
                "campaign_repo": repo_id,
                "campaign_session": session_id,
                "provenance": provenance,
            })
            if record.corrects is not None:
                metadata["corrects"] = record.corrects
                metadata["supersedes"] = [record.corrects]
            if record.known_at is not None and not self._fixture_clock_enabled:
                # The public engine write path stamps system time at dispatch.  Do
                # not rewrite that clock in production measurement; expose the
                # boundary so an unclocked temporal cell fails closed.
                self._counters["known_at_ingest_not_seeded"] += 1
            elif record.known_at is not None:
                self._counters["known_at_fixture_seeded"] += 1
            if record.operation == "event":
                event_id = self._run_clocked(
                    record,
                    lambda: self.engine.record_event(
                        "campaign",
                        record.content,
                        workspace_id=workspace_id,
                        repo_id=repo_id or "",
                        session_id=session_id or "",
                        refs=[record.record_id],
                    ),
                )
                metadata["event_id"] = str(event_id)
            if record.operation == "invalidate":
                target = self._memory_ids.get(record.record_id)
                if target is None:
                    raise AdapterError(
                        f"invalidation target is missing: {record.record_id}"
                    )
                def _invalidate() -> None:
                    with self.engine.store.write_transaction():
                        self.engine.store.close_validity(
                            target,
                            at=valid_at,
                            actor="campaign",
                            reason="campaign invalidation",
                        )
                self._run_clocked(record, _invalidate)
                result_ids.append(target)
                self._counters["invalidate"] += 1
                continue
            if record.operation == "correct":
                if not record.corrects or record.corrects not in self._memory_ids:
                    raise AdapterError(
                        f"correction target is missing: {record.corrects!r}"
                    )
                target = self._memory_ids[record.corrects]
                resolve_conflicts = False
            else:
                target = None
                resolve_conflicts = True
            mtype = MemoryType.EPISODIC if (
                record.role in {"assistant", "tool"} or record.operation == "event"
            ) else MemoryType.SEMANTIC
            result = self._run_clocked(
                record,
                lambda: self.engine.remember_with_resolution(
                    record.content,
                    workspace_id=workspace_id,
                    repo_id=repo_id,
                    session_id=session_id,
                    scope=scope,
                    mtype=mtype,
                    title=record.title,
                    metadata=metadata,
                    valid_from=valid_at,
                    resolve_conflicts=resolve_conflicts,
                    subject_key=record.subject_key,
                    claim_kind=record.claim_kind,
                ),
            )
            memory_id = str(result["id"])
            if target is not None:
                def _correct() -> None:
                    with self.engine.store.write_transaction():
                        self.engine.store.close_validity(
                            target,
                            at=valid_at,
                            actor="campaign",
                            reason="campaign correction",
                        )
                self._run_clocked(record, _correct)
            if valid_to is not None:
                def _close_fixture_interval() -> None:
                    with self.engine.store.write_transaction():
                        self.engine.store.close_validity(
                            memory_id,
                            at=valid_to,
                            actor="campaign",
                            reason="fixture validity boundary",
                        )
                self._run_clocked(record, _close_fixture_interval)
            self._memory_ids[record.record_id] = memory_id
            result_ids.append(memory_id)
            self._counters["ingest"] += 1
            if record.operation == "event":
                self._counters["event"] += 1
        return result_ids

    def recall(self, query: str, *, k: int = 10, token_budget: int = 1024,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None) -> AdapterRecall:
        self._ensure_prepared()
        self._check_filters(valid_at=valid_at, known_at=known_at)
        if (
            known_at is not None
            and not self._fixture_clock_enabled
            and self._counters.get("known_at_ingest_not_seeded", 0)
        ):
            raise AdapterCapabilityError(
                "Engraphis campaign fixture known_at cannot be applied without an "
                "injected system clock; production ingestion time was preserved"
            )
        _positive_int(k, name="k")
        _nonnegative_int(token_budget, name="token_budget")
        started = time.perf_counter()
        mapped_valid_at = self._map_fixture_time(valid_at)
        mapped_known_at = self._map_fixture_time(known_at)

        def _recall() -> Any:
            if self._arm_config is not None:
                from engraphis.core.interfaces import SearchFilter
                flt = SearchFilter(
                    workspace_id=self.workspace_id,
                    repo_id=self.repo_id,
                    session_id=self.session_id,
                    valid_at=mapped_valid_at,
                    known_at=mapped_known_at,
                    include_ancestors=True,
                )
                return self.engine.recall_engine.recall(
                    query,
                    flt,
                    k=max(1, k),
                    token_budget=token_budget,
                    retrieval_profile=self._baseline_spec.retrieval_profile,
                    arm_config=self._arm_config,
                    reinforce=False,
                )
            return self.engine.recall(
                query,
                workspace_id=self.workspace_id,
                repo_id=self.repo_id,
                session_id=self.session_id,
                valid_at=mapped_valid_at,
                known_at=mapped_known_at,
                k=max(1, k),
                token_budget=token_budget,
                reinforce=False,
            )

        result = self._run_recall_clocked(_recall)
        # Only packed chunks were admitted to the context.  Scored candidates
        # outside the pack are not evidence and cannot become gold citations.
        source_ids: list[str] = []
        for chunk in result.packed_chunks:
            memory_id = str(getattr(chunk, "id", ""))
            source_id = next(
                (record_id for record_id, candidate in self._memory_ids.items()
                 if candidate == memory_id),
                None,
            )
            if source_id is not None:
                source_ids.append(source_id)
        usage = result.usage
        context_tokens = int(getattr(usage, "context_tokens", _token_estimate(result.context)))
        source_tokens = int(getattr(usage, "source_tokens", _token_estimate(result.context)))
        packed_count = int(getattr(usage, "packed_count", len(result.packed_chunks)))
        omitted_count = int(getattr(usage, "omitted_count", 0))
        self._counters["recall"] += 1
        return AdapterRecall(
            context=result.context,
            source_ids=tuple(source_ids),
            usage=AdapterUsage(
                token_budget=token_budget,
                context_tokens=context_tokens,
                source_tokens=source_tokens,
                packed_count=packed_count,
                omitted_count=omitted_count,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                token_counter=str(getattr(usage, "token_counter", "unknown")),
            ),
            provenance={
                "adapter": self.capabilities.adapter,
                "retrieval_profile": getattr(result, "retrieval_profile", "balanced"),
                "historical": bool(getattr(result, "historical", False)),
                "valid_at": getattr(result, "valid_at", valid_at),
                "known_at": getattr(result, "known_at", known_at),
                "semantic_support": bool(getattr(result, "semantic_support", True)),
                "baseline_label": self.baseline_label,
                "source_ids_are_packed_only": True,
                "fixture_clock": (
                    {
                        "mode": "fixture_anchor",
                        "anchor": self._fixture_clock_anchor,
                        "logical_now": self._fixture_clock_current,
                        "mapped_valid_at": mapped_valid_at,
                        "mapped_known_at": mapped_known_at,
                    }
                    if self._fixture_clock_enabled else None
                ),
            },
        )

    def metrics(self) -> dict[str, Any]:
        result = super().metrics()
        result["baseline_label"] = self.baseline_label
        result["fixture_clock"] = (
            {
                "mode": "fixture_anchor",
                "anchor": self._fixture_clock_anchor,
                "logical_origin": self._fixture_clock_origin,
                "logical_now": self._fixture_clock_current,
            }
            if self._fixture_clock_enabled else {"mode": "production_wall_clock"}
        )
        return result

    def reset(self) -> None:
        if self._engine is not None and self._owned_engine:
            close = getattr(self._engine, "close", None)
            if callable(close):
                close()
        self._engine = None if self._owned_engine else self._engine
        self._fixture_clock_current = None
        self._fixture_clock_max = None
        self._session_ids.clear()
        super().reset()

    def close(self) -> None:
        if self._engine is not None:
            close = getattr(self._engine, "close", None)
            if callable(close):
                close()
            self._engine = None
        super().close()


class Mem0Adapter(_PeerAdapter):
    capabilities = AdapterCapabilities(
        adapter="mem0",
        version="2.0.20",
        source="https://github.com/mem0ai/mem0",
        source_revision="9a7924befd7026e41e445ba809370009e5e985a6",
        scopes=("workspace",),
        supports_valid_at=False,
        supports_known_at=False,
        supports_history=False,
        supports_graph=False,
        llm_requires_budgeted_client=True,
    )

    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Optional[Callable[..., Any]] = None,
        llm_client: Optional[BudgetedLLM] = None,
        config: Optional[Mapping[str, Any]] = None,
        reset_client: bool = False,
    ) -> None:
        super().__init__(config=config)
        self._client = client
        self._client_factory = client_factory
        self._llm_client = llm_client
        self._reset_client = bool(reset_client)
        self._llm_route: Optional[str] = None
        if llm_client is not None and not bool(getattr(llm_client, "is_budgeted", False)):
            raise AdapterConfigurationError("Mem0 LLM access must use a budgeted campaign client")
        if client_factory is not None and llm_client is None:
            raise AdapterConfigurationError(
                "Mem0 client_factory must receive the budgeted campaign LLM"
            )

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(
                config=dict(self._config), llm_client=self._llm_client,
            )
            return self._client
        if self._llm_client is None:
            raise AdapterConfigurationError(
                "Mem0 default provider route is forbidden; inject a configured client_factory "
                "whose LLM calls use the campaign budget client"
            )
        try:
            module = importlib.import_module("mem0")
            memory_cls = getattr(module, "Memory")
        except (ImportError, AttributeError) as exc:  # pragma: no cover
            raise AdapterDependencyError("mem0ai==2.0.20 is not installed") from exc
        # Do not allow Mem0 to select its OpenAI default embedder/vector store.  The
        # campaign caller must pin those local/explicit backends independently from
        # the budgeted extraction route.
        if not self._config.get("embedder") or not self._config.get("vector_store"):
            raise AdapterConfigurationError(
                "Mem0 requires explicit embedder and vector_store configuration"
            )
        route = _route_key("mem0", self)
        with _ROUTE_LOCK:
            _MEM0_ROUTES[route] = self._llm_client
        try:
            _register_mem0_budget_route(route, self._llm_client)
            # ``namespace`` belongs to the campaign adapter's physical
            # partitioning contract, not Mem0's strict MemoryConfig schema.
            memory_config = {
                key: value for key, value in self._config.items()
                if key not in {"namespace", "scope_partition"}
            }
            llm_config = dict(memory_config.get("llm", {}).get("config", {}))
            llm_config.update({
                "route_id": route,
                "model": "gpt-5.6-luna",
                "temperature": 0,
            })
            memory_config["llm"] = {
                # The pinned SDK's config validator accepts only built-in names;
                # the ``openai`` slot is rebound to the route class above.
                "provider": "openai",
                "config": llm_config,
            }
            constructor = getattr(memory_cls, "from_config", None)
            if not callable(constructor):
                raise AdapterDependencyError("Mem0 pinned SDK lacks Memory.from_config")
            self._client = constructor(memory_config)
            self._llm_route = route
            return self._client
        except AdapterError:
            with _ROUTE_LOCK:
                _MEM0_ROUTES.pop(route, None)
            _restore_mem0_budget_route(route)
            raise
        except Exception as exc:  # pragma: no cover - optional SDK/config surface
            with _ROUTE_LOCK:
                _MEM0_ROUTES.pop(route, None)
            _restore_mem0_budget_route(route)
            raise AdapterConfigurationError(
                "Mem0 could not be initialized with the pinned budgeted provider"
            ) from exc

    def ingest(self, records: Iterable[Mapping[str, Any]]) -> list[str]:
        self._ensure_prepared()
        normalized = self._normalize_records(records)
        # Perform all capability and scope checks before touching the provider;
        # a later unsupported record must not leave paid extraction side effects.
        self._preflight_records(normalized)
        if not normalized:
            return []
        add = getattr(self.client, "add", None)
        if not callable(add):
            raise AdapterError("Mem0 client has no add method")
        result_ids: list[str] = []
        for record in normalized:
            payload: Any = record.content
            if record.role:
                payload = [{"role": record.role, "content": record.content}]
            metadata = {
                **record.metadata,
                "campaign_record_id": record.record_id,
                "campaign_operation": record.operation,
                "campaign_scope": record.scope,
                "campaign_workspace": self._record_workspace(record),
                "campaign_workspace_label": record.workspace or self._workspace_label,
                "campaign_repo": record.repo or None,
                "campaign_session": record.session or None,
                "campaign_trusted": record.trusted,
                "campaign_partition": self._record_partition(record),
            }
            for field_name in ("timestamp", "valid_at", "valid_to", "known_at"):
                value = getattr(record, field_name)
                if value is not None:
                    metadata[f"campaign_{field_name}"] = value
            if any(getattr(record, field_name) is not None
                   for field_name in ("valid_at", "valid_to", "known_at")):
                self._counters["temporal_ingest_not_filterable"] += 1
            result = _call_with_fallbacks(add, (
                ((payload,), {"user_id": self._record_workspace(record), "metadata": metadata}),
                ((payload,), {"user_id": self._record_workspace(record)}),
                ((payload,), {"filters": {"user_id": self._record_workspace(record)}, "metadata": metadata}),
            ))
            result_items = _result_items(result)
            result_id = _value(result, "id", "memory_id", default=None)
            if result_id is None and result_items:
                result_id = _value(result_items[0], "id", "memory_id", default=None)
            result_ids.append(str(result_id or record.record_id))
            self._memory_ids[record.record_id] = result_ids[-1]
            self._record_partitions[record.record_id] = self._record_partition(record)
            self._backend_partitions[result_ids[-1]] = self._record_partition(record)
            self._record_trust[record.record_id] = record.trusted
            self._counters["ingest"] += 1
        return result_ids

    def recall(self, query: str, *, k: int = 10, token_budget: int = 1024,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None) -> AdapterRecall:
        self._ensure_prepared()
        self._check_filters(valid_at=valid_at, known_at=known_at)
        _positive_int(k, name="k")
        _nonnegative_int(token_budget, name="token_budget")
        started = time.perf_counter()
        search = getattr(self.client, "search", None)
        if not callable(search):
            raise AdapterError("Mem0 client has no search method")
        pages: list[list[Any]] = []
        for partition in self._selected_partitions:
            raw = _call_with_fallbacks(search, (
                ((query,), {"filters": {"user_id": partition}, "top_k": max(1, k), "threshold": 0.0, "rerank": False}),
                ((query,), {"user_id": partition, "top_k": max(1, k)}),
                ((query,), {"user_id": partition, "limit": max(1, k)}),
            ))
            pages.append(self._filter_partition_items(_result_items(raw)))
        items = _merge_peer_result_pages(pages)
        context, source_ids, usage, unmapped = _pack_peer_items(
            items,
            query=query,
            k=k,
            token_budget=token_budget,
            memory_ids=self._memory_ids,
            trust_by_id=self._record_trust,
        )
        self._counters["recall"] += 1
        usage = AdapterUsage(
            token_budget=usage.token_budget,
            context_tokens=usage.context_tokens,
            source_tokens=usage.source_tokens,
            packed_count=usage.packed_count,
            omitted_count=usage.omitted_count,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            token_counter=usage.token_counter,
        )
        return AdapterRecall(
            context=context,
            source_ids=source_ids,
            usage=usage,
            provenance={
                "adapter": self.capabilities.adapter,
                "source_revision": self.capabilities.source_revision,
                "scope_filter": {"user_id": list(self._selected_partitions)},
                "scope_partition": self._scope_partition,
                "scope_projection": (
                    "isolated_workspace_partition"
                    if self._scope_partition == "workspace"
                    else "isolated_repo_partition_without_session_history"
                ),
                "temporal_filter": "unsupported",
                "source_ids_are_packed_only": True,
                "unmapped_backend_results": unmapped,
            },
        )

    def reset(self) -> None:
        if self._reset_client and self._client is not None:
            reset = getattr(self._client, "reset", None)
            if callable(reset):
                _await(reset())
        super().reset()

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            _await(close())
        if self._llm_route is not None:
            with _ROUTE_LOCK:
                _MEM0_ROUTES.pop(self._llm_route, None)
            _restore_mem0_budget_route(self._llm_route)
            self._llm_route = None
        super().close()


class GraphitiAdapter(_PeerAdapter):
    capabilities = AdapterCapabilities(
        adapter="graphiti",
        version="0.29.3",
        source="https://github.com/getzep/graphiti",
        source_revision="021d3a57d511f21b10adaf7fa923bd5c1fce5e9d",
        scopes=("workspace",),
        supports_valid_at=False,
        supports_known_at=False,
        supports_history=False,
        supports_graph=True,
        llm_requires_budgeted_client=True,
    )

    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Optional[Callable[..., Any]] = None,
        llm_client: Optional[BudgetedLLM] = None,
        config: Optional[Mapping[str, Any]] = None,
        reset_client: bool = False,
    ) -> None:
        super().__init__(config=config)
        self._client = client
        self._client_factory = client_factory
        self._llm_client = llm_client
        self._reset_client = bool(reset_client)
        self._llm_route: Optional[str] = None
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._indices_initialized = False
        if llm_client is not None and not bool(getattr(llm_client, "is_budgeted", False)):
            raise AdapterConfigurationError("Graphiti LLM access must use a budgeted campaign client")
        if client_factory is not None and llm_client is None:
            raise AdapterConfigurationError(
                "Graphiti client_factory must receive the budgeted campaign LLM"
            )

    def _run_async(self, value: Any) -> Any:
        """Run one Graphiti coroutine on the adapter's persistent event loop.

        graphiti-core wraps the Neo4j async driver; creating a fresh loop for each
        synchronous adapter call leaves pooled sockets bound to a closed loop and
        makes ``close()`` fail.  Campaign execution is synchronous, so one loop per
        adapter gives constructor, ingest/search, and shutdown the same ownership.
        """
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._async_loop is None or self._async_loop.is_closed():
                self._async_loop = asyncio.new_event_loop()
            return self._async_loop.run_until_complete(value)
        raise AdapterError("Graphiti async operation cannot run inside an active event loop")

    def _ensure_indices(self) -> None:
        """Initialize Graphiti indexes once before any episode operation."""
        if self._indices_initialized or self._client is None:
            return
        build = getattr(self._client, "build_indices_and_constraints", None)
        if callable(build):
            # graphiti-core uses idempotent CREATE ... IF NOT EXISTS statements;
            # no LLM route is involved in this schema/bootstrap operation.
            self._run_async(build())
        self._indices_initialized = True

    @property
    def client(self) -> Any:
        if self._client is not None:
            self._ensure_indices()
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(
                config=dict(self._config), llm_client=self._llm_client,
            )
            self._ensure_indices()
            return self._client
        if self._llm_client is None:
            raise AdapterConfigurationError(
                "Graphiti default provider route is forbidden; inject a configured client_factory "
                "whose LLM calls use the campaign budget client"
            )
        try:
            module = importlib.import_module("graphiti_core")
            graphiti_cls = getattr(module, "Graphiti")
        except (ImportError, AttributeError) as exc:  # pragma: no cover
            raise AdapterDependencyError("graphiti-core==0.29.3 is not installed") from exc
        # Graphiti otherwise creates OpenAI defaults for the embedder and cross
        # encoder.  Requiring explicit objects keeps every networked component
        # visible to the campaign configuration.
        if not self._config.get("embedder") or not self._config.get("cross_encoder"):
            raise AdapterConfigurationError(
                "Graphiti requires explicit embedder and cross_encoder configuration"
            )
        route = _route_key("graphiti", self)
        routed_llm = _register_graphiti_budget_route(route, self._llm_client)
        try:
            # ``namespace`` is consumed by _PeerAdapter.prepare and is not a
            # constructor argument in graphiti-core 0.29.3.
            graphiti_config = {
                key: value for key, value in self._config.items()
                if key not in {"namespace", "scope_partition"}
            }
            graphiti_config.pop("embedder", None)
            graphiti_config.pop("cross_encoder", None)
            graphiti_config["llm_client"] = routed_llm
            graphiti_config["embedder"] = self._config["embedder"]
            graphiti_config["cross_encoder"] = self._config["cross_encoder"]
            if "graph_driver" not in graphiti_config and not graphiti_config.get("uri"):
                raise AdapterConfigurationError(
                    "Graphiti requires a graph_driver or explicit Neo4j uri"
                )
            self._llm_route = route
            self._client = graphiti_cls(**graphiti_config)
            self._ensure_indices()
            return self._client
        except AdapterError:
            with _ROUTE_LOCK:
                _GRAPHITI_ROUTES.pop(route, None)
            raise
        except Exception as exc:  # pragma: no cover - optional SDK/config surface
            with _ROUTE_LOCK:
                _GRAPHITI_ROUTES.pop(route, None)
            raise AdapterConfigurationError(
                "Graphiti could not be initialized with the pinned budgeted provider"
            ) from exc

    def ingest(self, records: Iterable[Mapping[str, Any]]) -> list[str]:
        self._ensure_prepared()
        normalized = self._normalize_records(records)
        # Fail before the first graph/LLM operation if this corpus asks for a
        # scope or history feature Graphiti cannot represent.
        self._preflight_records(normalized)
        if not normalized:
            return []
        add_episode = getattr(self.client, "add_episode", None)
        if not callable(add_episode):
            raise AdapterError("Graphiti client has no add_episode method")
        result_ids: list[str] = []
        for record in normalized:
            reference = datetime.fromtimestamp(
                record.timestamp if record.timestamp is not None else time.time(),
                tz=timezone.utc,
            )
            source_description = (
                record.title or "campaign fixture"
            ) + (
                f"; campaign_record_id={record.record_id}; operation={record.operation}; "
                f"trusted={str(record.trusted).lower()}; valid_at={record.valid_at}; "
                f"valid_to={record.valid_to}; known_at={record.known_at}; "
                f"workspace={record.workspace or self._workspace_label}; "
                f"repo={record.repo}; session={record.session}"
            )
            partition = self._record_partition(record)
            kwargs: dict[str, Any] = {
                "name": record.record_id,
                "episode_body": record.content,
                "source_description": source_description,
                "reference_time": reference,
                "group_id": partition,
            }
            result = self._run_async(add_episode(**kwargs))
            episode = _value(result, "episode", default=None)
            result_id = _value(episode, "uuid", "id", default=None)
            if result_id is None:
                result_id = _value(result, "uuid", "id", default=record.record_id)
            result_ids.append(str(result_id))
            self._memory_ids[record.record_id] = result_ids[-1]
            self._record_partitions[record.record_id] = partition
            self._backend_partitions[result_ids[-1]] = partition
            self._record_trust[record.record_id] = record.trusted
            self._counters["ingest"] += 1
        return result_ids

    def recall(self, query: str, *, k: int = 10, token_budget: int = 1024,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None) -> AdapterRecall:
        self._ensure_prepared()
        self._check_filters(valid_at=valid_at, known_at=known_at)
        _positive_int(k, name="k")
        _nonnegative_int(token_budget, name="token_budget")
        started = time.perf_counter()
        search = getattr(self.client, "search", None)
        if not callable(search):
            raise AdapterError("Graphiti client has no search method")
        raw: Any = _call_with_fallbacks(
            search,
            (
                ((query,), {"group_ids": list(self._selected_partitions), "num_results": max(1, k)}),
                ((query,), {"group_ids": list(self._selected_partitions), "limit": max(1, k)}),
            ),
            await_result=self._run_async,
        )
        items = self._filter_partition_items(_result_items(raw))
        context, source_ids, usage, unmapped = _pack_peer_items(
            items,
            query=query,
            k=k,
            token_budget=token_budget,
            memory_ids=self._memory_ids,
            trust_by_id=self._record_trust,
        )
        self._counters["recall"] += 1
        usage = AdapterUsage(
            token_budget=usage.token_budget,
            context_tokens=usage.context_tokens,
            source_tokens=usage.source_tokens,
            packed_count=usage.packed_count,
            omitted_count=usage.omitted_count,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            token_counter=usage.token_counter,
        )
        return AdapterRecall(
            context=context,
            source_ids=source_ids,
            usage=usage,
            provenance={
                "adapter": self.capabilities.adapter,
                "source_revision": self.capabilities.source_revision,
                "group_ids": list(self._selected_partitions),
                "scope_partition": self._scope_partition,
                "scope_projection": (
                    "isolated_workspace_partition"
                    if self._scope_partition == "workspace"
                    else "isolated_repo_partition_without_session_history"
                ),
                "temporal_filter": "unsupported",
                "source_ids_are_packed_only": True,
                "unmapped_backend_results": unmapped,
            },
        )

    def reset(self) -> None:
        if self._reset_client and self._client is not None:
            reset = getattr(self._client, "reset", None)
            if callable(reset):
                self._run_async(reset())
        super().reset()

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            self._run_async(close())
        if self._llm_route is not None:
            with _ROUTE_LOCK:
                _GRAPHITI_ROUTES.pop(self._llm_route, None)
            self._llm_route = None
        if self._async_loop is not None:
            self._async_loop.close()
            self._async_loop = None
        self._client = None
        self._indices_initialized = False
        super().close()


def load_competitor_pins(path: str | Path = PINS_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "engraphis-competitor-pins/v1":
        raise AdapterConfigurationError("competitor pins file has an unsupported schema")
    competitors = payload.get("competitors")
    if not isinstance(competitors, Mapping):
        raise AdapterConfigurationError("competitor pins file has no competitor map")
    for name in ("mem0", "graphiti"):
        entry = competitors.get(name)
        if not isinstance(entry, Mapping):
            raise AdapterConfigurationError(f"competitor pin is missing: {name}")
        for field_name in ("package", "package_version", "release_tag", "source"):
            if not isinstance(entry.get(field_name), str) or not entry[field_name].strip():
                raise AdapterConfigurationError(f"competitor pin field is invalid: {name}.{field_name}")
        revision = entry.get("source_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise AdapterConfigurationError(f"competitor pin revision is invalid: {name}")
    return payload


def create_adapter(name: str, *, config: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> CampaignAdapter:
    normalized = str(name).strip().casefold().replace("-", "_")
    if normalized in {"engraphis", "local"}:
        return EngraphisAdapter(engine_kwargs=config, **kwargs)
    if normalized == "mem0":
        return Mem0Adapter(config=config, **kwargs)
    if normalized == "graphiti":
        return GraphitiAdapter(config=config, **kwargs)
    raise AdapterConfigurationError(f"unknown campaign adapter: {name}")


__all__ = [
    "AdapterCapabilities", "AdapterCapabilityError", "AdapterConfigurationError",
    "AdapterDependencyError", "AdapterError", "AdapterRecall", "AdapterUsage",
    "CampaignAdapter", "CampaignRecord", "EngraphisAdapter", "GraphitiAdapter",
    "Mem0Adapter", "build_graphiti_local_components", "build_mem0_local_config",
    "create_adapter", "load_competitor_pins",
]
