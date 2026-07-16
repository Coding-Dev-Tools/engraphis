from engraphis.core.conflicts import detect_conflicts
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope


def _mem(mid: str, text: str, *, title: str = "") -> MemoryRecord:
    return MemoryRecord(id=mid, content=text, title=title, mtype=MemoryType.SEMANTIC,
                        scope=Scope.WORKSPACE, workspace_id="w")


def test_detects_duplicate_without_mutating_anything():
    old = _mem("mem_old", "The API uses PASETO tokens for authentication.")
    new = _mem("mem_new", "The API uses PASETO tokens for authentication.")
    conflicts = detect_conflicts(new, [old])
    assert conflicts[0].type == "duplicate"
    assert conflicts[0].memory_id == "mem_old"
    assert conflicts[0].suggested_resolution == "reinforce/noop existing memory"
    assert conflicts[0].to_dict()["evidence"]["overlap"] >= 0.82


def test_detects_negation_contradiction_on_same_subject():
    old = _mem("mem_old", "The API uses JWT tokens for authentication.")
    new = _mem("mem_new", "The API does not use JWT tokens for authentication.")
    conflicts = detect_conflicts(new, [old])
    assert conflicts[0].type == "contradiction"
    assert conflicts[0].severity >= 0.78
    assert "opposite polarity" in conflicts[0].reason or conflicts[0].evidence["polarity_mismatch"]


def test_detects_object_replacement_as_obsolete_when_temporal():
    old = _mem("mem_old", "The API uses JWT for auth.")
    new = _mem("mem_new", "As of July, the API uses PASETO for auth instead.")
    conflicts = detect_conflicts(new, [old])
    assert conflicts[0].type == "obsolete"
    assert conflicts[0].suggested_resolution == "invalidate older memory"
    assert conflicts[0].evidence["predicate"] == "use"
    assert "JWT" in conflicts[0].evidence["old_object"]
    assert "PASETO" in conflicts[0].evidence["new_object"]


def test_detects_numeric_value_conflict():
    old = _mem("mem_old", "The API rate limit is 100 requests per minute.")
    new = _mem("mem_new", "The API rate limit is 500 requests per minute.")
    conflicts = detect_conflicts(new, [old])
    assert conflicts[0].type == "contradiction"
    assert conflicts[0].suggested_resolution == "review value conflict"
    assert conflicts[0].evidence["old_values"] == ["100requests"]
    assert conflicts[0].evidence["new_values"] == ["500requests"]


def test_detects_refinement_when_new_fact_adds_specificity():
    old = _mem("mem_old", "The API uses PASETO tokens.")
    new = _mem("mem_new", "The API uses PASETO tokens with Ed25519 keys and hourly rotation.")
    conflicts = detect_conflicts(new, [old])
    assert conflicts[0].type == "refinement"
    assert conflicts[0].suggested_resolution.startswith("link as refinement")
    assert conflicts[0].evidence["containment"] >= 0.72


def test_ignores_unrelated_memories_and_sorts_by_severity():
    duplicate = _mem("mem_dup", "Now the API uses PASETO for auth instead.")
    obsolete = _mem("mem_old", "The API uses JWT for auth.")
    unrelated = _mem("mem_other", "The billing dashboard uses monthly invoices.")
    new = _mem("mem_new", "Now the API uses PASETO for auth instead.")
    conflicts = detect_conflicts(new, [duplicate, unrelated, obsolete])
    assert [c.memory_id for c in conflicts] == ["mem_old", "mem_dup"]
    assert conflicts[0].type == "obsolete"
    assert all(c.memory_id != "mem_other" for c in conflicts)
