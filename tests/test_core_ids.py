import pytest

from engraphis.core import ids


def test_prefix_and_shape():
    mid = ids.new_id("memory")
    assert mid.startswith("mem_")
    assert len(mid.split("_")[1]) == 26


def test_unknown_kind_falls_back_to_kind_as_prefix():
    with pytest.raises(ValueError):
        ids.new_id("widget")
    assert ids.new_id("widget", allow_unsafe=True).startswith("widget_")


def test_ulid_is_time_sortable():
    early = ids.ulid(timestamp_ms=1_000)
    late = ids.ulid(timestamp_ms=2_000_000_000_000)
    assert early < late


_CROCKFORD = {
    char: index for index, char in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
}


def _decode_timestamp(value):
    decoded = 0
    for char in value[:10]:
        decoded = decoded * 32 + _CROCKFORD[char]
    return decoded


@pytest.mark.parametrize(
    "timestamp_ms",
    [
        pytest.param(-1, id="negative"),
        pytest.param(1 << 48, id="overflow"),
        pytest.param(True, id="boolean"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="text"),
    ],
)
def test_ulid_rejects_timestamps_outside_the_48_bit_domain(timestamp_ms):
    with pytest.raises(ValueError, match=r"\[0, 2\*\*48\)"):
        ids.ulid(timestamp_ms=timestamp_ms)


@pytest.mark.parametrize("timestamp_ms", [0, (1 << 48) - 1])
def test_ulid_accepts_and_round_trips_48_bit_boundaries(timestamp_ms):
    value = ids.ulid(timestamp_ms=timestamp_ms)
    assert len(value) == 26
    assert value[0] in "01234567"
    assert _decode_timestamp(value) == timestamp_ms


def test_ids_are_unique():
    seen = {ids.new_id("memory") for _ in range(5000)}
    assert len(seen) == 5000
