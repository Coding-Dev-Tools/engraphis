"""Exact-value input failures retain the public validation error contract."""
import pytest

from engraphis.service import MemoryService, ValidationError


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(("content", "value", "value_type"), [
    ("Deployment token is Δ-42.", "absent", "identifier"),
    ("Deployment token is Δ-42.", "Δ-42", "unsupported-type"),
    ("Δ-42 or Δ-42", "Δ-42", "identifier"),
])
def test_invalid_exact_values_fail_validation_before_single_or_batch_write(
    batch, content, value, value_type,
):
    service = MemoryService.create(":memory:", graph_extractor="none")
    with pytest.raises(ValidationError, match="exact_value"):
        if batch:
            service.remember_many([
                {"content": "A preceding valid fact."},
                {"content": content, "exact_value": value, "exact_value_type": value_type},
            ], workspace="acme")
        else:
            service.remember(
                content, workspace="acme", exact_value=value, exact_value_type=value_type,
            )
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
