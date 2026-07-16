import pytest

from engraphis.backends.retention import get_retention_supervisor
from engraphis.core.engine import MemoryEngine
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


def test_unknown_retention_backend_is_actionable():
    with pytest.raises(ValueError, match="none.*llm"):
        get_retention_supervisor("mystery")
