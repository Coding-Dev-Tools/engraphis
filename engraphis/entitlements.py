"""Read-only mirror of the private hosted entitlement contract.

This module answers one question: which plan *advertises* a hosted capability. It never
authorizes anything. Every paid operation is still gated on the control plane's response,
and the dashboard only renders what the contract declares.

Keeping one machine-readable mirror here -- instead of three hand-maintained copies in the
client and its tests -- is the point. ``scripts/check_commercial_manifest.py`` compares this
file with the private contract in CI, so a capability that exists only server-side fails the
release gate instead of silently disappearing from a paying customer's dashboard.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional


SCHEMA = "engraphis-entitlement-contract/v1"
PLANS = ("free", "pro", "team")
STATUSES = ("available", "free", "declared")
CONTRACT_PATH = Path(__file__).with_name("entitlement_contract.json")

_MAX_KEY_CHARS = 64
_MAX_UNRECOGNIZED = 32


@lru_cache(maxsize=1)
def contract() -> dict:
    """Return the validated contract, cached for the life of the process."""

    data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    _validate(data)
    return data


def reload() -> None:
    """Drop the cached contract. Tests and release tooling only."""

    contract.cache_clear()


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError("entitlement contract: " + message)


def _validate(data: object) -> None:
    """Reject a malformed contract before any caller can misread it.

    The checks are deliberately structural rather than a JSON Schema dependency: the core
    must keep running on the numpy-only floor, and a clear ``ValueError`` is more useful to
    a release engineer than a schema-library stack trace.
    """

    _require(isinstance(data, dict), "root must be a JSON object")
    _require(data.get("schema") == SCHEMA, "schema must be " + SCHEMA)

    plans = data.get("plans")
    _require(isinstance(plans, dict), "plans must be an object")
    _require(set(plans) == set(PLANS), "plans must name free, pro and team")

    features = data.get("features")
    _require(isinstance(features, dict) and features, "features must be a non-empty object")

    for plan in PLANS:
        names = plans[plan]
        _require(isinstance(names, list), "plans.%s must be a list" % plan)
        _require(
            all(isinstance(name, str) and name.strip() for name in names),
            "plans.%s must contain non-empty feature names" % plan,
        )
        _require(
            len(names) == len(set(names)),
            "plans.%s must not repeat a feature" % plan,
        )
        for name in names:
            spec = features.get(name)
            _require(
                isinstance(spec, dict),
                "plans.%s names an unknown feature: %s" % (plan, name),
            )
            # ``plan`` is the *minimum* plan that grants a feature; Team inherits Pro.
            declared_plan = spec.get("plan")
            if plan == "team":
                _require(
                    declared_plan in ("pro", "team"),
                    "plans.team contains %s, which declares plan %r"
                    % (name, declared_plan),
                )
            else:
                _require(
                    declared_plan == plan,
                    "plans.%s contains %s, which declares plan %r"
                    % (plan, name, declared_plan),
                )
            _require(
                spec.get("status") == "available",
                "plans.%s contains %s, which is not available" % (plan, name),
            )

    _require(
        set(plans["pro"]).issubset(set(plans["team"])),
        "the team plan must include every pro feature",
    )

    for name, spec in features.items():
        _require(
            isinstance(name, str) and name.strip() and len(name) <= _MAX_KEY_CHARS,
            "feature names must be non-empty and at most %d characters" % _MAX_KEY_CHARS,
        )
        _require(isinstance(spec, dict), "feature %s must be an object" % name)
        _require(spec.get("plan") in PLANS, "feature %s names an unknown plan" % name)
        _require(
            isinstance(spec.get("label"), str) and bool(spec["label"].strip()),
            "feature %s needs a non-empty label" % name,
        )
        _require(
            spec.get("status") in STATUSES,
            "feature %s has an unknown status" % name,
        )
        if spec["status"] == "free":
            _require(
                spec["plan"] == "free",
                "free feature %s must resolve to the free plan" % name,
            )
            _require(
                name not in plans["pro"] and name not in plans["team"],
                "free feature %s must not be advertised as paid" % name,
            )
        if spec["status"] == "available":
            _require(
                name in plans[spec["plan"]],
                "available feature %s is missing from plans.%s" % (name, spec["plan"]),
            )
            if spec["plan"] == "pro":
                _require(
                    name in plans["team"],
                    "available pro feature %s is missing from plans.team" % name,
                )
        if spec["status"] == "declared":
            _require(
                not any(name in names for names in plans.values()),
                "declared feature %s must not be granted until it is available" % name,
            )

    included = data.get("included_services", {})
    _require(isinstance(included, dict), "included_services must be an object")
    for name, spec in included.items():
        _require(
            isinstance(name, str) and name.strip() and len(name) <= _MAX_KEY_CHARS,
            "included service names must be non-empty and bounded",
        )
        _require(isinstance(spec, dict), "included service %s must be an object" % name)
        _require(spec.get("plan") in PLANS, "included service %s names an unknown plan" % name)
        _require(
            isinstance(spec.get("label"), str) and bool(spec["label"].strip()),
            "included service %s needs a non-empty label" % name,
        )
        calls = spec.get("calls_per_person_per_day")
        if calls is not None:
            _require(
                isinstance(calls, int) and not isinstance(calls, bool) and calls > 0,
                "included service %s calls_per_person_per_day must be a positive integer"
                % name,
            )

    limits = data.get("limits")
    _require(isinstance(limits, dict), "limits must be an object")
    days = limits.get("trial_days_by_plan")
    _require(
        isinstance(days, dict) and set(days) == {"pro", "team"},
        "limits.trial_days_by_plan must name pro and team",
    )
    for plan, value in days.items():
        _require(
            isinstance(value, int) and not isinstance(value, bool) and value > 0,
            "limits.trial_days_by_plan.%s must be a positive integer" % plan,
        )
    grace = limits.get("grace_hours")
    _require(
        isinstance(grace, int) and not isinstance(grace, bool) and grace > 0,
        "limits.grace_hours must be a positive integer",
    )


def features_for(plan: object) -> list:
    """Return the hosted features a plan advertises, sorted and never guessed.

    ``local`` is the client's own alias for an unconnected installation and resolves to the
    free plan, exactly as the server treats an unknown or unpaid plan.
    """

    key = str(plan or "").strip().lower()
    if key == "local":
        key = "free"
    return sorted(contract()["plans"].get(key, ()))


def feature_spec(feature: object) -> Optional[dict]:
    """Return one feature's contract entry, or ``None`` when this build does not know it."""

    key = str(feature or "").strip().lower()
    if not key or len(key) > _MAX_KEY_CHARS:
        return None
    spec = contract()["features"].get(key)
    return spec if isinstance(spec, dict) else None


def required_plans() -> dict:
    """Return every known capability mapped to the minimum plan that grants it."""

    plans = {name: str(spec["plan"]) for name, spec in contract()["features"].items()}
    plans.update({
        name: str(spec["plan"])
        for name, spec in contract().get("included_services", {}).items()
    })
    return plans


def included_services() -> dict:
    """Return benefits included with a paid plan but not gated as hosted features.

    These are informational: the server still authorizes every call. Keeping them out of
    the feature-row list prevents a tick-box that implies a live grant when the account
    portal may have a different allowance state.
    """

    return {
        name: dict(spec)
        for name, spec in contract().get("included_services", {}).items()
    }


def required_plan(feature: object) -> str:
    """Return the advertised minimum plan for a feature.

    Unknown names keep the historical ``pro`` fallback so a new server key never tells a
    Team customer to buy Pro. Callers that need to distinguish "unknown" from "Pro" use
    :func:`feature_spec`.
    """

    spec = feature_spec(feature)
    return str(spec["plan"]) if spec else "pro"


def labels() -> dict:
    """Return the labels for features this build can actually render."""

    return {
        name: str(spec["label"])
        for name, spec in contract()["features"].items()
        if spec.get("status") == "available"
    }


def is_known(feature: object) -> bool:
    """Return whether this build has any contract entry for *feature*."""

    return feature_spec(feature) is not None


def unrecognized(granted: object, plan: object) -> list:
    """Return granted keys this build does not know, bounded and sorted.

    A future server release may grant a capability this client predates. The grant must
    never become a feature row (it cannot be rendered or verified here), but staying silent
    is how a paying customer ends up with a lock over something they bought. The caller
    surfaces this list as "update Engraphis to use it" instead.
    """

    if not isinstance(granted, (list, tuple)):
        return []
    values = set()
    for item in granted:
        if not isinstance(item, str):
            continue
        key = item.strip().lower()
        if key and len(key) <= _MAX_KEY_CHARS:
            values.add(key)
    if "automation" in values:
        values.update(("consolidation", "dreaming"))
    allowed = set(features_for(plan))
    known = contract()["features"]
    return sorted(name for name in values if name not in allowed and name not in known)[
        :_MAX_UNRECOGNIZED
    ]


def trial_days_by_plan() -> dict:
    """Return the disclosed trial duration for each paid plan."""

    days = contract()["limits"]["trial_days_by_plan"]
    return {plan: int(days[plan]) for plan in ("pro", "team")}


def grace_hours() -> int:
    """Return the disclosed hosted-account write-grace window."""

    return int(contract()["limits"]["grace_hours"])
