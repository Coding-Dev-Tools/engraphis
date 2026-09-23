"""Invalid replay mutations cannot change canonical benchmark history."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

import pytest

from eval import coding_corpus as corpus


def _op(
    op: str,
    evidence_id: str,
    *,
    scope: str = "workspace",
    workspace: str = "ws-a",
    repo: str = "repo-a",
    session: str = "session-a",
    valid_from: float = 10.0,
    valid_to: Optional[float] = None,
    corrects: Optional[str] = None,
):
    return corpus.SessionOperation(
        op=op,
        evidence_id=evidence_id,
        content="payload" if op != "invalidate" else "",
        scope=scope,
        workspace=workspace,
        repo=repo,
        session=session,
        trusted=True,
        valid_from=valid_from,
        valid_to=valid_to,
        known_at=valid_from,
        corrects=corrects,
    )


def _seed(ledger, **kwargs):
    operation = _op("remember", "old", **kwargs)
    ledger.apply(operation)
    return operation


def test_duplicate_correction_is_rejected_before_predecessor_close():
    ledger = corpus.SessionLedger()
    _seed(ledger)
    ledger.apply(_op("remember", "already-used", valid_from=11.0))
    events_before = list(ledger.events)

    with pytest.raises(ValueError, match="duplicate correction evidence id"):
        ledger.apply(_op("correct", "already-used", valid_from=20.0, corrects="old"))

    assert ledger._records["old"].valid_to is None
    assert "already-used" in ledger._records
    assert ledger.events == events_before


@pytest.mark.parametrize(
    ("scope", "target", "mutation"),
    [
        ("workspace", {"workspace": "ws-a"}, {"workspace": "ws-b"}),
        ("repo", {"workspace": "ws-a", "repo": "repo-a"}, {"repo": "repo-b"}),
        ("session", {"workspace": "ws-a", "repo": "repo-a", "session": "s-a"}, {"session": "s-b"}),
    ],
)
@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_history_mutations_reject_cross_effective_scope_before_state_change(
    scope, target, mutation, operation,
):
    ledger = corpus.SessionLedger()
    _seed(ledger, scope=scope, **target)
    events_before = list(ledger.events)
    values = {"scope": scope, **target, **mutation, "valid_from": 20.0}
    if operation == "correct":
        values.update(evidence_id="new", corrects="old")
    else:
        values.update(evidence_id="old")

    with pytest.raises(ValueError, match="effective scope"):
        ledger.apply(_op(operation, values.pop("evidence_id"), **values))

    assert ledger._records["old"].valid_to is None
    assert "new" not in ledger._records
    assert ledger.events == events_before


@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_history_mutations_reject_backdated_time_before_state_change(operation):
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0)
    events_before = list(ledger.events)
    values = {"valid_from": 9.0}
    if operation == "correct":
        values.update(evidence_id="new", corrects="old")
    else:
        values.update(evidence_id="old")

    with pytest.raises(ValueError, match="predates"):
        ledger.apply(_op(operation, values.pop("evidence_id"), **values))

    assert ledger._records["old"].valid_to is None
    assert "new" not in ledger._records
    assert ledger.events == events_before


def test_repeated_invalidation_keeps_earliest_close_and_records_requests():
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0)

    ledger.apply(_op("invalidate", "old", valid_from=20.0))
    ledger.apply(_op("invalidate", "old", valid_from=30.0))
    assert ledger._records["old"].valid_to == 20.0

    ledger.apply(_op("invalidate", "old", valid_from=15.0))
    assert ledger._records["old"].valid_to == 15.0
    assert [event["op"] for event in ledger.events] == [
        "remember", "invalidate", "invalidate", "invalidate",
    ]


@pytest.mark.parametrize("operation", ["remember", "event"])
def test_new_records_reject_inverted_intervals_before_any_mutation(operation):
    ledger = corpus.SessionLedger()

    with pytest.raises(ValueError, match="valid_to"):
        ledger.apply(_op(operation, "bad", valid_from=10.0, valid_to=9.0))

    assert ledger._records == {}
    assert ledger.events == []


@pytest.mark.parametrize("field", ["valid_from", "valid_to"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_new_records_reject_nonfinite_temporal_fields_before_any_mutation(field, value):
    ledger = corpus.SessionLedger()
    values = {"valid_from": 10.0, "valid_to": None}
    values[field] = value

    with pytest.raises(ValueError, match="must be finite"):
        ledger.apply(_op("remember", "bad", **values))

    assert ledger._records == {}
    assert ledger.events == []


def test_new_records_allow_zero_length_intervals():
    ledger = corpus.SessionLedger()

    ledger.apply(_op("remember", "empty", valid_from=10.0, valid_to=10.0))

    assert ledger._records["empty"].valid_to == 10.0
    assert [event["id"] for event in ledger.events] == ["empty"]


def test_correction_rejects_inverted_successor_before_predecessor_close():
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0)
    events_before = list(ledger.events)

    with pytest.raises(ValueError, match="valid_to"):
        ledger.apply(_op("correct", "new", valid_from=20.0, valid_to=19.0, corrects="old"))

    assert ledger._records["old"].valid_to is None
    assert "new" not in ledger._records
    assert ledger.events == events_before


@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_history_mutations_reject_nonfinite_time_before_state_change(operation):
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0)
    events_before = list(ledger.events)
    values = {"valid_from": float("nan")}
    if operation == "correct":
        values.update(evidence_id="new", corrects="old")
    else:
        values.update(evidence_id="old")

    with pytest.raises(ValueError, match="must be finite"):
        ledger.apply(_op(operation, values.pop("evidence_id"), **values))

    assert ledger._records["old"].valid_to is None
    assert "new" not in ledger._records
    assert ledger.events == events_before


@pytest.mark.parametrize(
    ("scope", "target", "mutation"),
    [
        ("workspace", {"workspace": "ws-a", "repo": "repo-a", "session": "s-a"},
         {"repo": "repo-b", "session": "s-b"}),
        ("repo", {"workspace": "ws-a", "repo": "repo-a", "session": "s-a"},
         {"session": "s-b"}),
        ("session", {"workspace": "ws-a", "repo": "repo-a", "session": "s-a"}, {}),
    ],
)
def test_same_effective_scope_correction_remains_supported(scope, target, mutation):
    ledger = corpus.SessionLedger()
    _seed(ledger, scope=scope, **target)
    values = {"scope": scope, **target, **mutation, "valid_from": 20.0}

    ledger.apply(_op("correct", "new", corrects="old", **values))

    assert ledger._records["old"].valid_to == 20.0
    assert ledger._records["new"].valid_from == 20.0


def test_closed_correction_keeps_existing_already_closed_rule():
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0, valid_to=20.0)
    events_before = list(ledger.events)

    with pytest.raises(ValueError, match="already closed"):
        ledger.apply(_op("correct", "new", valid_from=20.0, corrects="old"))

    assert ledger._records["old"].valid_to == 20.0
    assert "new" not in ledger._records
    assert ledger.events == events_before


@pytest.mark.parametrize("field,value", [
    ("valid_from", None), ("valid_from", True), ("valid_from", "10"),
    ("valid_from", 10 ** 1000), ("valid_to", 10 ** 1000),
    ("known_at", float("nan")), ("known_at", float("inf")), ("known_at", True),
])
def test_invalid_temporal_types_and_system_times_leave_no_record(field, value):
    ledger = corpus.SessionLedger()
    operation = replace(_op("remember", "bad"), **{field: value})
    with pytest.raises(ValueError, match="must be finite"):
        ledger.apply(operation)
    assert ledger._records == {}
    assert ledger.events == []


@pytest.mark.parametrize("correction_at", [10.0, 20.0])
def test_correction_can_start_at_predecessor_start_or_before_its_future_close(correction_at):
    ledger = corpus.SessionLedger()
    _seed(ledger, valid_from=10.0, valid_to=30.0)
    ledger.apply(_op("correct", "new", corrects="old", valid_from=correction_at))
    assert ledger._records["old"].valid_to == correction_at
    assert ledger._records["new"].valid_from == correction_at


@pytest.mark.parametrize("field", ["valid_from", "known_at"])
def test_loaded_nonfinite_timestamp_is_rejected_during_replay(field):
    operation = corpus.Corpus._operation({
        "op": "remember", "evidence_id": "bad", "content": "fact", field: float("nan"),
    }, "fixture")
    ledger = corpus.SessionLedger()
    with pytest.raises(ValueError, match="must be finite"):
        ledger.apply(operation)
    assert ledger._records == {}
    assert ledger.events == []


def test_unsupported_scope_cannot_enter_the_replay_ledger():
    ledger = corpus.SessionLedger()
    with pytest.raises(ValueError, match="unsupported session scope"):
        ledger.apply(_op("remember", "bad", scope="unknown"))
    assert ledger._records == {}
    assert ledger.events == []
