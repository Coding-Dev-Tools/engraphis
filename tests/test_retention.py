import pytest

from engraphis.backends.retention import LLMRetentionSupervisor, get_retention_supervisor
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, RetentionDecision
from engraphis.service import MemoryService, ValidationError


@pytest.mark.parametrize(
    ("label", "importance", "stability"),
    [("ephemeral", 0.1, 0.25), ("normal", 0.5, 1.0), ("critical", 0.9, 8.0)],
)
def test_host_retention_class_is_bounded_and_never_drops_the_write(
        label, importance, stability):
    service = MemoryService.create(":memory:", retention_supervisor="none")
    out = service.remember(
        f"{label} retention candidate",
        workspace="acme",
        scope="workspace",
        retention_class=label,
        retention_reason="host classification",
    )
    record = service.store.get_memory(out["id"])
    assert record is not None
    assert record.importance == importance
    assert record.stability == stability
    assert record.metadata["retention_supervision"]["label"] == label
    assert out["receipt"]["metadata"]["retention"] == label


def test_caller_metadata_cannot_smuggle_retention_supervision():
    """`metadata.retention_supervision` is a reserved service-internal channel:
    the engine trusts it as a host decision with raw importance/stability, so an
    API caller injecting it via the metadata dict would bypass the bounded
    `retention_class` presets entirely (stability 100 vs the critical preset's
    8.0) — a memory-poisoning persistence amplifier (PR #19 review follow-up)."""
    service = MemoryService.create(":memory:", retention_supervisor="none")
    out = service.remember(
        "smuggled persistence",
        workspace="acme",
        scope="workspace",
        metadata={"retention_supervision": {
            "label": "critical", "retain": True,
            "importance": 1.0, "stability": 100.0,
        }, "note": "kept"},
    )
    record = service.store.get_memory(out["id"])
    assert record is not None
    # Defaults apply: the injected decision was stripped, not honored.
    assert record.importance == 0.0
    assert record.stability == 1.0
    assert "retention_supervision" not in record.metadata
    # Unreserved metadata keys survive the strip.
    assert record.metadata["note"] == "kept"


def test_sanctioned_retention_class_still_beats_smuggled_metadata():
    """When both are supplied, the validated `retention_class` preset wins and
    the smuggled raw values are discarded."""
    service = MemoryService.create(":memory:", retention_supervisor="none")
    out = service.remember(
        "critical but bounded",
        workspace="acme",
        scope="workspace",
        retention_class="critical",
        metadata={"retention_supervision": {
            "label": "critical", "retain": True, "stability": 100.0,
        }},
    )
    record = service.store.get_memory(out["id"])
    assert record is not None
    assert record.importance == 0.9
    assert record.stability == 8.0  # the preset, not the smuggled 100.0


def test_explicit_importance_is_a_floor_for_retention_supervision():
    service = MemoryService.create(":memory:", retention_supervisor="none")
    out = service.remember(
        "User-marked important transient note.",
        workspace="acme",
        scope="workspace",
        importance=0.8,
        retention_class="ephemeral",
    )
    assert service.store.get_memory(out["id"]).importance == 0.8


def test_invalid_retention_class_is_rejected():
    service = MemoryService.create(":memory:", retention_supervisor="none")
    with pytest.raises(ValidationError):
        service.remember(
            "candidate", workspace="acme", retention_class="delete-immediately"
        )


def test_non_finite_importance_is_rejected():
    service = MemoryService.create(":memory:", retention_supervisor="none")
    with pytest.raises(ValidationError, match="finite"):
        service.remember("candidate", workspace="acme", importance=float("nan"))


def test_supervisor_failure_degrades_to_default_retention():
    class BrokenSupervisor:
        def decide(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    engine = MemoryEngine.create(":memory:", retention_supervisor="none")
    engine.retention_supervisor = BrokenSupervisor()
    wid = engine.store.get_or_create_workspace("acme")
    mid = engine.remember("Keep the write even if supervision fails.", workspace_id=wid)
    record = engine.store.get_memory(mid)
    assert record.importance == 0.0
    assert record.stability == 1.0
    assert "retention_supervision" not in record.metadata


def test_non_finite_supervisor_values_fall_back_to_label_presets():
    class NonFiniteSupervisor:
        def decide(self, *args, **kwargs):
            return RetentionDecision(
                label="critical", importance=float("nan"), stability=float("inf")
            )

    engine = MemoryEngine.create(":memory:", retention_supervisor="none")
    engine.retention_supervisor = NonFiniteSupervisor()
    wid = engine.store.get_or_create_workspace("acme")
    mid = engine.remember("Critical policy.", workspace_id=wid)
    record = engine.store.get_memory(mid)
    assert record.importance == 0.9
    assert record.stability == 8.0


def test_llm_supervisor_rejects_non_finite_or_wrongly_typed_output():
    class BadLLM:
        def extract_json(self, *args, **kwargs):
            return {
                "label": "critical", "retain": "false",
                "importance": float("nan"), "stability": float("inf"),
                "reason": "unsafe\x1b[31m",
            }

    with pytest.raises(ValueError):
        LLMRetentionSupervisor(BadLLM()).decide(
            "candidate", mtype=MemoryType.SEMANTIC
        )


def test_unknown_retention_backend_is_actionable():
    with pytest.raises(ValueError, match="none.*llm"):
        get_retention_supervisor("mystery")
