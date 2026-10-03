"""Opt-in Jev selection among routes from the deterministic planner."""
from __future__ import annotations

import math
import time
from typing import Optional

from engraphis.backends.jev_decision import JevDecisionBackend
from engraphis.core.interfaces import PlannedQuery, RetrievalPlan, SearchFilter
from engraphis.core.query_planner import DeterministicQueryPlanner, MAX_PLANNED_QUERIES


class JevAssistedQueryPlanner:
    """Keep local route generation canonical; Jev may only prioritize one route.

    The original query is always first and unchanged. Jev never creates query text,
    changes memory filters, or removes deterministic routes. Without per-call remote
    consent, with an uncertain result, or on any failure, the deterministic plan is
    returned as-is with a stable reason code for the caller to display.
    """

    identity = "engraphis.query-planner.jev-assisted.v1"

    def __init__(
        self, decision_backend: Optional[JevDecisionBackend] = None,
        deterministic: Optional[DeterministicQueryPlanner] = None,
    ) -> None:
        self.decision_backend = decision_backend
        self.deterministic = deterministic or DeterministicQueryPlanner()

    def plan(
        self, query: str, *, filter: Optional[SearchFilter] = None,
        timeout_s: Optional[float] = None,
    ) -> RetrievalPlan:
        return self.deterministic.plan(
            query, filter=filter, timeout_s=timeout_s, mode="auto",
        )

    def plan_with_jev(
        self, query: str, *, filter: Optional[SearchFilter] = None,
        timeout_s: Optional[float] = None, allow_remote: bool = False,
        data_classification: str = "internal",
    ) -> RetrievalPlan:
        started = time.monotonic()
        baseline = self.plan(query, filter=filter, timeout_s=timeout_s)
        # The standard deterministic planner normalizes whitespace for route
        # generation. Keep the exact caller query as Jev's mandatory route and in
        # the bounded route-selection context.
        if baseline.queries:
            original_route = baseline.queries[0]
            baseline = RetrievalPlan(
                queries=(
                    PlannedQuery(
                        text=query,
                        priority=original_route.priority,
                        profile=original_route.profile,
                        mtypes=original_route.mtypes,
                    ),
                    *baseline.queries[1:MAX_PLANNED_QUERIES],
                ),
                mtype_limits=dict(baseline.mtype_limits),
                reason_codes=baseline.reason_codes,
            )
        alternatives = list(baseline.queries[1:MAX_PLANNED_QUERIES])
        if not alternatives:
            return self._with_reason(baseline, "jev_no_alternatives")
        if len(alternatives) < 2:
            # A single alternate offers no selection to make and would fail the
            # decision client's minimum two-option contract.
            return self._with_reason(baseline, "jev_no_route_choice")
        if allow_remote is not True:
            return self._with_reason(baseline, "jev_remote_consent_required")
        if data_classification not in {"public", "internal"}:
            return self._with_reason(baseline, "jev_invalid_classification")
        if self.decision_backend is None:
            return self._with_reason(baseline, "jev_backend_unavailable")
        decision_timeout = timeout_s
        if timeout_s is not None:
            if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                    or timeout_s <= 0 or timeout_s > 15):
                return self._with_reason(baseline, "jev_deadline_exhausted")
            elapsed = max(0.0, time.monotonic() - started)
            decision_timeout = min(float(timeout_s), float(timeout_s) - elapsed)
            if decision_timeout <= 0:
                return self._with_reason(baseline, "jev_deadline_exhausted")

        options = tuple(f"route_{index}" for index in range(1, len(alternatives) + 1))
        state = "\n\n".join(
            [f"ORIGINAL QUERY:\n{query}"]
            + [f"{option}: {route.text}" for option, route in zip(options, alternatives)]
        )
        decision = self.decision_backend.choose_option(
            state,
            question_id="route",
            prompt="Which deterministic alternate route should receive the highest retrieval priority?",
            options=options,
            allow_remote=allow_remote,
            purpose="custom",
            data_classification=data_classification,
            timeout_s=decision_timeout,
        )
        if decision.status != "decision" or decision.value not in options:
            reason = decision.fallback_reason
            status = "jev_uncertain" if decision.status == "uncertain" else _fallback_code(reason)
            return self._with_reason(baseline, status)

        chosen = alternatives[options.index(str(decision.value))]
        remaining = [route for route in alternatives if route is not chosen]
        routes = [baseline.queries[0], chosen, *remaining]
        prioritized = tuple(
            PlannedQuery(
                route.text,
                priority=(1 if index == 0 else index + 1),
                profile=route.profile,
                mtypes=route.mtypes,
            )
            for index, route in enumerate(routes[:MAX_PLANNED_QUERIES])
        )
        return RetrievalPlan(
            queries=prioritized,
            mtype_limits=dict(baseline.mtype_limits),
            reason_codes=(*baseline.reason_codes[:7], "jev_route_selected"),
        )

    @staticmethod
    def _with_reason(plan: RetrievalPlan, reason: str) -> RetrievalPlan:
        return RetrievalPlan(
            queries=plan.queries[:MAX_PLANNED_QUERIES],
            mtype_limits=dict(plan.mtype_limits),
            reason_codes=(*plan.reason_codes[:7], reason),
        )


def _fallback_code(reason: Optional[str]) -> str:
    allowed = {
        "remote_not_authorized": "jev_remote_consent_required",
        "invalid_data_classification": "jev_invalid_classification",
        "invalid_input": "jev_invalid_input",
        "input_too_large": "jev_input_too_large",
        "deadline_exhausted": "jev_deadline_exhausted",
        "client_deadline_unsupported": "jev_client_deadline_unsupported",
        "sensitive_content": "jev_sensitive_content",
        "backend_unavailable": "jev_backend_unavailable",
        "client_contract_invalid": "jev_client_contract_invalid",
        "provider_fallback": "jev_provider_fallback",
        "remote_unavailable": "jev_remote_unavailable",
        "malformed_response": "jev_malformed_response",
    }
    return allowed.get(reason or "", "jev_fallback")
