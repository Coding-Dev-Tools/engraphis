"""Exact-model, budgeted Responses API client for benchmark campaigns.

The client has one dispatch path and zero retries.  A reservation is written to
``CampaignLedger`` before the transport is called.  Any exception after that
point becomes an uncertain terminal call, so a resumed campaign cannot issue a
duplicate provider request.
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from eval.campaign_ledger import (
    BudgetApproval,
    CampaignLedger,
    CampaignLedgerError,
    CallReservation,
    canonical_json,
    sha256_text,
)


MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "medium"
# ``responses`` remains the identity used by historical injected test doubles
# and frozen API-era artifacts.  Hosted campaign construction is OAuth-only;
# ``approved_client`` supplies the successor transport explicitly.
DEFAULT_TRANSPORT_IDENTITY = "responses"
DEFAULT_BILLING_BASIS = "api_price_proxy_not_invoice"
INPUT_MICROS_PER_MILLION = 200_000
CACHED_INPUT_MICROS_PER_MILLION = 20_000
CACHE_WRITE_MICROS_PER_MILLION = 250_000
OUTPUT_MICROS_PER_MILLION = 1_200_000
TOKEN_COUNTER = "engraphis.utf8bytes.v1"
# Keep campaign requests below the first higher-context pricing tier and bound
# accidental fixture explosions before a provider request is dispatched.
MAX_INPUT_TOKENS = 128_000


class CampaignAPIError(RuntimeError):
    """A safe client error that contains no provider response body."""


class CampaignDependencyError(CampaignAPIError):
    """An optional SDK dependency is unavailable."""


class CampaignUncertainCall(CampaignAPIError):
    """The provider call may have executed and cannot be retried automatically."""


class ResponsesTransport(Protocol):
    """Minimal injected transport; tests never need the OpenAI package."""

    def create(self, **kwargs: Any) -> Any:
        ...


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_nonnegative_int(value: Any, *, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or type(value) not in (int, float):
        raise CampaignAPIError(f"provider usage field {name} is invalid")
    if not math.isfinite(float(value)) or int(value) != value or value < 0:
        raise CampaignAPIError(f"provider usage field {name} is invalid")
    return int(value)


def _nested_usage(
    usage: Any, name: str, nested: str, nested_name: Optional[str] = None,
) -> int:
    value = _field(usage, name)
    if value is not None:
        return _safe_nonnegative_int(value, name=name)
    detail = _field(usage, nested, {})
    return _safe_nonnegative_int(_field(detail, nested_name or name, 0), name=name)


def _required_usage(usage: Any, name: str, nested: str, nested_name: Optional[str] = None) -> int:
    """Read a required provider counter without silently converting absence to zero."""
    value = _field(usage, name)
    if value is None:
        detail = _field(usage, nested, {})
        value = _field(detail, nested_name or name)
    if value is None:
        raise CampaignAPIError(f"Responses API returned incomplete usage: {name}")
    return _safe_nonnegative_int(value, name=name)


def _jsonable_input(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable_input(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_jsonable_input(item) for item in value]
    return str(value)


def input_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(_jsonable_input(value)).encode("utf-8")).hexdigest()


def estimate_input_tokens(value: Any) -> int:
    """Conservative deterministic fallback for a canonical JSON request.

    UTF-8 bytes are an upper bound on tokenizer pieces for ordinary BPE/SentencePiece
    encodings and remain conservative for CJK and other multibyte source text.  The
    JSON envelope includes instructions and structured-output schema when supplied.
    """
    encoded = canonical_json(_jsonable_input(value))
    return max(1, len(encoded.encode("utf-8", "surrogatepass")))


def _ceil_total_micros(raw_micros: int) -> int:
    """Round a sum of category prices up once to integer microdollars."""
    if raw_micros <= 0:
        return 0
    return (raw_micros + 999_999) // 1_000_000


def estimate_cost_micros(
    *,
    input_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int,
    approval: Optional[BudgetApproval] = None,
) -> int:
    """Estimate integer-microdollar cost without double-counting input tokens.

    ``cache_write_tokens`` is a billing-category estimate for a covered subset
    of the input, rather than an additional token stream.  The remaining input
    is priced as ordinary or cached input, so a partial write estimate cannot
    hide the uncovered ordinary tokens.  All category prices are summed before
    one upward integer-microdollar rounding, matching the campaign proposal.
    Completed-call accounting remains separate from unverified cache-write
    billing.
    """
    approval = approval or BudgetApproval.create(max_calls=1, max_cost_micros=10**18)
    for name, value in (
        ("input_tokens", input_tokens), ("cached_input_tokens", cached_input_tokens),
        ("cache_write_tokens", cache_write_tokens), ("output_tokens", output_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CampaignAPIError(f"{name} must be a non-negative integer")
    if cached_input_tokens > input_tokens:
        raise CampaignAPIError("cached_input_tokens cannot exceed input_tokens")
    if cached_input_tokens + cache_write_tokens > input_tokens:
        raise CampaignAPIError(
            "cached_input_tokens plus cache_write_tokens cannot exceed input_tokens"
        )
    ordinary_input_tokens = input_tokens - cached_input_tokens - cache_write_tokens
    raw_micros = (
        ordinary_input_tokens * approval.input_micros_per_million
        + cached_input_tokens * approval.cached_input_micros_per_million
        + cache_write_tokens * approval.cache_write_micros_per_million
        + output_tokens * approval.output_micros_per_million
    )
    return _ceil_total_micros(raw_micros)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    latency_ms: float
    cost_micros: int
    worst_case_cost_micros: int
    cache_write_tokens_assumed: int
    token_counter: str = TOKEN_COUNTER
    transport_identity: str = DEFAULT_TRANSPORT_IDENTITY
    billing_basis: str = DEFAULT_BILLING_BASIS

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "cost_micros": self.cost_micros,
            "worst_case_cost_micros": self.worst_case_cost_micros,
            "cache_write_tokens_assumed": self.cache_write_tokens_assumed,
            "token_counter": self.token_counter,
            "transport_identity": self.transport_identity,
            "billing_basis": self.billing_basis,
        }


@dataclass(frozen=True)
class LunaResponse:
    text: str
    usage: TokenUsage
    provenance: dict[str, Any] = field(default_factory=dict)


def _response_text(response: Any) -> str:
    output_text = _field(response, "output_text")
    if isinstance(output_text, str):
        return output_text
    output = _field(response, "output", ())
    pieces: list[str] = []
    for item in output or ():
        content = _field(item, "content", ())
        for part in content or ():
            text = _field(part, "text")
            if isinstance(text, str):
                pieces.append(text)
    if pieces:
        return "".join(pieces)
    raise CampaignAPIError("Responses API returned no text output")


def _provider_usage(response: Any) -> tuple[int, int, int, int, int]:
    usage = _field(response, "usage")
    if usage is None:
        raise CampaignAPIError("Responses API returned no usage accounting")
    input_tokens = _required_usage(usage, "input_tokens", "input_tokens_details")
    cached = _nested_usage(
        usage, "cached_input_tokens", "input_tokens_details", "cached_tokens"
    )
    output_tokens = _required_usage(usage, "output_tokens", "output_tokens_details")
    reasoning = _nested_usage(usage, "reasoning_tokens", "output_tokens_details")
    total_value = _field(usage, "total_tokens")
    if total_value is None:
        raise CampaignAPIError("Responses API returned incomplete usage: total_tokens")
    total = _safe_nonnegative_int(total_value, name="total_tokens")
    if cached > input_tokens:
        raise CampaignAPIError("Responses API cached usage exceeds input usage")
    if total < input_tokens + output_tokens:
        raise CampaignAPIError("Responses API total usage is below input plus output")
    return input_tokens, cached, output_tokens, reasoning, total


class _OpenAIResponsesTransport:
    """Retained only as a marker for historical API-era artifacts.

    The live campaign no longer constructs or accepts this route.  Keeping the
    class name avoids making old private artifacts unreadable while making an
    accidental API-key dispatch fail before importing the SDK.
    """

    def __init__(self, client: Any = None) -> None:
        raise CampaignAPIError(
            "the API-key Responses transport is disabled; inject the Codex OAuth transport"
        )

    def create(self, **kwargs: Any) -> Any:
        responses = getattr(self.client, "responses", None)
        create = getattr(responses, "create", None)
        if not callable(create):
            raise CampaignAPIError("the configured client has no Responses API")
        return create(**kwargs)


class LunaResponsesClient:
    """Budgeted exact GPT-5.6 Luna Responses client.

    ``transport`` is injectable for deterministic tests.  Competitor adapters
    must receive this client's ``complete`` callable (or another object with
    ``is_budgeted=True``) when their ingestion/extraction path uses an LLM.
    """

    is_budgeted = True
    model = MODEL
    reasoning_effort = REASONING_EFFORT

    def __init__(
        self,
        ledger: CampaignLedger,
        *,
        transport: Optional[ResponsesTransport] = None,
        retries: int = 0,
    ) -> None:
        if retries != 0:
            raise CampaignAPIError("campaign API retries are disabled; resume only completed calls")
        if transport is None:
            raise CampaignAPIError(
                "campaign transport must be injected explicitly; hosted execution is Codex OAuth-only"
            )
        if isinstance(transport, _OpenAIResponsesTransport):
            raise CampaignAPIError(
                "the API-key Responses transport is disabled; use Codex OAuth"
            )
        self.ledger = ledger
        self.transport = transport
        self.transport_identity = str(
            getattr(transport, "identity", DEFAULT_TRANSPORT_IDENTITY)
        )
        self.billing_basis = str(
            getattr(transport, "billing_basis", DEFAULT_BILLING_BASIS)
        )
        if not self.transport_identity or not self.billing_basis:
            raise CampaignAPIError("campaign transport provenance is incomplete")

    def complete(
        self,
        *,
        call_id: str,
        kind: str,
        input: Any,
        max_output_tokens: int,
        input_tokens: Optional[int] = None,
        cached_input_tokens: int = 0,
        cache_write_tokens: Optional[int] = None,
        instructions: Optional[str] = None,
        text: Optional[Mapping[str, Any]] = None,
    ) -> LunaResponse:
        if kind not in {"ingest", "reader", "evaluator", "correction"}:
            raise CampaignAPIError("kind must be ingest, reader, evaluator, or correction")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
            raise CampaignAPIError("max_output_tokens must be a non-negative integer")
        if max_output_tokens <= 0:
            raise CampaignAPIError("max_output_tokens must be a positive integer")
        reservation_input = {
            "input": input,
            "instructions": instructions,
            "text": text,
        }
        estimated_request_tokens = estimate_input_tokens(reservation_input)
        if input_tokens is not None and (
            isinstance(input_tokens, bool) or not isinstance(input_tokens, int)
            or input_tokens < 0
        ):
            raise CampaignAPIError("input_tokens must be a non-negative integer")
        # An explicit tokenizer count may cover only ``input``.  The envelope
        # (instructions and structured-output schema) is therefore always added
        # conservatively through the max with our deterministic envelope count.
        resolved_input_tokens = (
            estimated_request_tokens if input_tokens is None
            else max(input_tokens, estimated_request_tokens)
        )
        if isinstance(resolved_input_tokens, bool) or not isinstance(resolved_input_tokens, int):
            raise CampaignAPIError("input_tokens must be a non-negative integer")
        if resolved_input_tokens < 0:
            raise CampaignAPIError("input_tokens must be a non-negative integer")
        if resolved_input_tokens > MAX_INPUT_TOKENS:
            raise CampaignAPIError(
                f"input exceeds the conservative {MAX_INPUT_TOKENS}-token campaign limit"
            )
        if isinstance(cached_input_tokens, bool) or not isinstance(cached_input_tokens, int):
            raise CampaignAPIError("cached_input_tokens must be a non-negative integer")
        if cached_input_tokens < 0:
            raise CampaignAPIError("cached_input_tokens must be a non-negative integer")
        # Cache hints are unverified before dispatch. The default reservation
        # therefore retains full-input write coverage even when a hint is given.
        # An explicit partial count bounds the assumed write coverage only; it
        # does not establish actual provider cache-write billing.
        resolved_cache_write = (
            resolved_input_tokens if cache_write_tokens is None else cache_write_tokens
        )
        if isinstance(resolved_cache_write, bool) or not isinstance(resolved_cache_write, int):
            raise CampaignAPIError("cache_write_tokens must be a non-negative integer")
        if resolved_cache_write < 0:
            raise CampaignAPIError("cache_write_tokens must be a non-negative integer")
        estimated_cost = estimate_cost_micros(
            input_tokens=resolved_input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=(
                max(0, resolved_input_tokens - cached_input_tokens)
                if cache_write_tokens is None else resolved_cache_write
            ),
            output_tokens=max_output_tokens,
            approval=self.ledger.approval,
        )
        # Price the uncovered input at both cache-hit extremes. Configurable
        # rates may also price ordinary or cached input above a write, so cover
        # both possible known-usage extremes before any transport call.
        unverified_cache_ceiling = max(
            estimate_cost_micros(
                input_tokens=resolved_input_tokens,
                cached_input_tokens=cached,
                cache_write_tokens=resolved_cache_write,
                output_tokens=max_output_tokens,
                approval=self.ledger.approval,
            )
            for cached in (0, resolved_input_tokens - resolved_cache_write)
        )
        known_usage_ceiling = max(
            estimate_cost_micros(
                input_tokens=resolved_input_tokens,
                cached_input_tokens=cached,
                output_tokens=max_output_tokens,
                approval=self.ledger.approval,
            )
            for cached in (0, resolved_input_tokens)
        )
        estimated_cost = max(estimated_cost, unverified_cache_ceiling, known_usage_ceiling)
        request_hash = input_digest({
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "kind": kind,
            "input": input,
            "instructions": instructions,
            "text": text,
            "max_output_tokens": max_output_tokens,
        })
        reservation = self.ledger.reserve(
            call_id, kind, estimated_cost, request_sha256=request_hash,
        )
        if reservation.status == "completed":
            return self._resumed_response(
                reservation,
                max_output_tokens=max_output_tokens,
                resolved_input_tokens=resolved_input_tokens,
            )
        if reservation.status == "uncertain":
            raise CampaignUncertainCall(
                f"campaign call {call_id} is uncertain and will not be retried"
            )
        if reservation.status == "failed":
            raise CampaignAPIError(f"campaign call {call_id} is terminally failed")

        payload: dict[str, Any] = {
            "model": MODEL,
            "reasoning": {"effort": REASONING_EFFORT},
            "input": input,
            "max_output_tokens": max_output_tokens,
            "store": False,
            "tools": [],
        }
        if self.transport_identity == "codex_oauth":
            # The OAuth transport journals this binding before dispatch so a
            # crash cannot leave an unidentifiable provider call to replay.
            # Keep these campaign-only fields out of historical/API-shaped
            # injected transports and their request contracts.
            payload["benchmark_call_id"] = call_id
            payload["benchmark_request_sha256"] = request_hash
        if instructions is not None:
            payload["instructions"] = instructions
        if text is not None:
            payload["text"] = dict(text)
        self.ledger.mark_dispatched(call_id)
        started = time.perf_counter()
        try:
            response = self.transport.create(**payload)
            response_model = _field(response, "model")
            if response_model != MODEL:
                raise CampaignAPIError("Responses API returned a different model")
            benchmark_provenance = _field(response, "benchmark_provenance", {})
            if benchmark_provenance is None:
                benchmark_provenance = {}
            if not isinstance(benchmark_provenance, Mapping):
                raise CampaignAPIError("campaign transport returned invalid provenance")
            if self.transport_identity == "codex_oauth":
                expected_provenance = {
                    "transport": self.transport_identity,
                    "billing_basis": self.billing_basis,
                    "model_verification": "native_thread_start_and_no_model_reroute",
                    "automatic_retries": 0,
                    "requested_model": MODEL,
                    "effective_model": MODEL,
                    "reasoning_effort": REASONING_EFFORT,
                }
                for key, expected in expected_provenance.items():
                    if benchmark_provenance.get(key) != expected:
                        raise CampaignAPIError(
                            f"Codex OAuth transport provenance mismatch: {key}"
                        )
            output = _response_text(response)
            input_used, cached_used, output_used, reasoning_used, total_used = _provider_usage(response)
            if output_used > max_output_tokens:
                raise CampaignAPIError("Responses API exceeded max_output_tokens")
            if input_used > resolved_input_tokens:
                raise CampaignAPIError("Responses API input exceeded the reserved estimate")
            if total_used > resolved_input_tokens + max_output_tokens:
                raise CampaignAPIError("Responses API total usage exceeded the reserved limit")
        except CampaignAPIError as exc:
            # A response that arrived but violated the frozen contract is a
            # known terminal failure.  Persist that state before surfacing it,
            # so a resumed run cannot dispatch the same call again.
            error_class = "provider_contract_error"
            message = str(exc).casefold()
            if "different model" in message:
                error_class = "model_mismatch"
            elif "max_output" in message:
                error_class = "output_limit_exceeded"
            elif "input exceeded" in message:
                error_class = "input_estimate_exceeded"
            elif "total usage" in message:
                error_class = "total_limit_exceeded"
            try:
                self.ledger.mark_failed(call_id, error_class=error_class)
            except CampaignLedgerError:
                pass
            raise
        except Exception as exc:
            try:
                self.ledger.mark_uncertain(call_id)
            except CampaignLedgerError:
                pass
            raise CampaignUncertainCall(
                f"campaign call {call_id} has no durable completion"
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        known_cost = estimate_cost_micros(
            input_tokens=input_used,
            cached_input_tokens=cached_used,
            cache_write_tokens=0,
            output_tokens=output_used,
            approval=self.ledger.approval,
        )
        usage = TokenUsage(
            input_tokens=input_used,
            cached_input_tokens=cached_used,
            output_tokens=output_used,
            reasoning_output_tokens=reasoning_used,
            total_tokens=total_used,
            latency_ms=latency_ms,
            cost_micros=known_cost,
            worst_case_cost_micros=estimated_cost,
            cache_write_tokens_assumed=resolved_cache_write,
            transport_identity=self.transport_identity,
            billing_basis=self.billing_basis,
        )
        try:
            self.ledger.complete(
                call_id,
                output,
                usage=usage.as_dict(),
                actual_cost_micros=known_cost,
            )
        except CampaignLedgerError as exc:
            # The provider response is no longer replay-safe if durable
            # completion failed.  Marking it uncertain preserves the
            # no-duplicate-call boundary for a resumed campaign.
            try:
                self.ledger.mark_uncertain(call_id, error_class="ledger_completion_error")
            except CampaignLedgerError:
                pass
            raise CampaignUncertainCall(
                f"campaign call {call_id} has no durable completion"
            ) from exc
        response_id = _field(response, "id")
        provenance = {
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "transport": self.transport_identity,
            "transport_identity": self.transport_identity,
            "billing_basis": self.billing_basis,
            "cost_basis": "api_price_proxy",
            "request_sha256": request_hash,
            "response_id_sha256": sha256_text(str(response_id)) if response_id else None,
            "known_cost_micros": known_cost,
            "worst_case_cost_micros": estimated_cost,
            "cache_write_tokens_assumed": resolved_cache_write,
            "billing_basis_detail": "known_usage_excludes_cache_write; reservation_is_ceiling",
            "retries": 0,
        }
        return LunaResponse(text=output, usage=usage, provenance=provenance)

    def _resumed_response(
        self, reservation: CallReservation, *, max_output_tokens: int,
        resolved_input_tokens: int,
    ) -> LunaResponse:
        if reservation.response is None or reservation.usage is None:
            raise CampaignAPIError("completed campaign call has no resumable response")
        raw = reservation.usage
        if not isinstance(raw, Mapping):
            raise CampaignAPIError("completed campaign call has invalid usage accounting")
        transport_identity = raw.get("transport_identity", DEFAULT_TRANSPORT_IDENTITY)
        billing_basis = raw.get("billing_basis", DEFAULT_BILLING_BASIS)
        if transport_identity != self.transport_identity or billing_basis != self.billing_basis:
            raise CampaignAPIError("completed campaign call accounting provenance differs")
        required = ("input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens", "total_tokens")
        if any(key not in raw for key in required):
            raise CampaignAPIError("completed campaign call has incomplete usage accounting")
        input_tokens = _safe_nonnegative_int(raw["input_tokens"], name="input_tokens")
        cached_input_tokens = _safe_nonnegative_int(
            raw["cached_input_tokens"], name="cached_input_tokens"
        )
        output_tokens = _safe_nonnegative_int(raw["output_tokens"], name="output_tokens")
        reasoning_output_tokens = _safe_nonnegative_int(
            raw["reasoning_output_tokens"], name="reasoning_output_tokens"
        )
        total_tokens = _safe_nonnegative_int(raw["total_tokens"], name="total_tokens")
        if cached_input_tokens > input_tokens:
            raise CampaignAPIError("completed campaign cached usage exceeds input usage")
        if total_tokens < input_tokens + output_tokens:
            raise CampaignAPIError("completed campaign total usage is below input plus output")
        if output_tokens > max_output_tokens:
            raise CampaignAPIError("completed campaign output exceeds max_output_tokens")
        if input_tokens > resolved_input_tokens:
            raise CampaignAPIError("completed campaign input exceeds its reserved estimate")
        if total_tokens > resolved_input_tokens + max_output_tokens:
            raise CampaignAPIError("completed campaign total usage exceeds its reserved limit")
        usage = TokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            total_tokens=total_tokens,
            latency_ms=float(raw.get("latency_ms", 0.0)),
            cost_micros=int(raw.get("cost_micros", reservation.estimated_cost_micros)),
            worst_case_cost_micros=int(raw.get("worst_case_cost_micros", reservation.estimated_cost_micros)),
            cache_write_tokens_assumed=int(raw.get("cache_write_tokens_assumed", 0)),
            token_counter=str(raw.get("token_counter", TOKEN_COUNTER)),
            transport_identity=transport_identity,
            billing_basis=billing_basis,
        )
        return LunaResponse(
            text=reservation.response,
            usage=usage,
            provenance={
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "transport": self.transport_identity,
                "transport_identity": self.transport_identity,
                "billing_basis": self.billing_basis,
                "cost_basis": "api_price_proxy",
                "resumed": True,
                "known_cost_micros": usage.cost_micros,
                "worst_case_cost_micros": usage.worst_case_cost_micros,
                "cache_write_tokens_assumed": usage.cache_write_tokens_assumed,
                "billing_basis_detail": "known_usage_excludes_cache_write; reservation_is_ceiling",
                "retries": 0,
            },
        )


def require_budgeted_llm(client: Any) -> Any:
    """Reject competitor clients that would silently call their default provider."""
    if client is None or not bool(getattr(client, "is_budgeted", False)):
        raise CampaignAPIError(
            "competitor LLM access requires the campaign budgeted Responses client"
        )
    return client


__all__ = [
    "CACHE_WRITE_MICROS_PER_MILLION", "CampaignAPIError", "CampaignDependencyError",
    "CampaignUncertainCall", "CACHED_INPUT_MICROS_PER_MILLION", "INPUT_MICROS_PER_MILLION",
    "LunaResponse", "LunaResponsesClient", "MODEL", "OUTPUT_MICROS_PER_MILLION",
    "MAX_INPUT_TOKENS", "REASONING_EFFORT", "ResponsesTransport", "TOKEN_COUNTER", "TokenUsage",
    "estimate_cost_micros", "estimate_input_tokens", "input_digest", "require_budgeted_llm",
]
