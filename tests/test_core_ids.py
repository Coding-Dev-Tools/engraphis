from engraphis.core import ids


def test_prefix_and_shape():
    mid = ids.new_id("memory")
    assert mid.startswith("mem_")
    assert len(mid.split("_")[1]) == 26


def test_unknown_kind_falls_back_to_kind_as_prefix():
    assert ids.new_id("widget").startswith("widget_")


def test_ulid_is_time_sortable():
    early = ids.ulid(timestamp_ms=1_000)
    late = ids.ulid(timestamp_ms=2_000_000_000_000)
    assert early < late


def test_ids_are_unique():
    seen = {ids.new_id("memory") for _ in range(5000)}
    assert len(seen) == 5000
