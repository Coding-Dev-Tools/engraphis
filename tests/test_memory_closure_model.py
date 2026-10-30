import math
import sqlite3

import pytest

from engraphis.core import scoring
from engraphis.core.interfaces import (
    MAX_CLOSURE_OBSERVATIONS,
    MemoryRecord,
    normalize_closure_observations,
)
from engraphis.core.schema import (
    BROWSE_SCHEMA_SQL,
    MEMORY_CLOSURE_SCHEMA_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)


def database(*, install_closures=True):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.executescript(BROWSE_SCHEMA_SQL)
    if install_closures:
        conn.executescript(MEMORY_CLOSURE_SCHEMA_SQL)
    return conn


def insert_memory(conn, endpoint=None, recorded=None):
    conn.execute(
        "INSERT INTO memories(id,workspace_id,content,valid_from,ingested_at,"
        "valid_to,valid_to_recorded_at) VALUES ('mem_review','ws_review','example',100,100,?,?)",
        (endpoint, recorded),
    )
    conn.commit()


def observations(conn):
    return tuple(conn.execute(
        "SELECT valid_to,recorded_at FROM memory_closures "
        "WHERE memory_id='mem_review' ORDER BY valid_to,recorded_at"
    ).fetchall())


def revision(conn):
    return conn.execute("SELECT revision FROM browse_scope_revisions").fetchone()[0]


def test_canonical_union_dedup_and_null_order():
    assert normalize_closure_observations(
        [[300, 400], [200, 500], [300, None], [300, 400]],
        valid_to=200, valid_to_recorded_at=500,
    ) == ((200.0, 500.0), (300.0, None), (300.0, 400.0))


@pytest.mark.parametrize("value", [
    True, "[]", {}, [(None, 100)], [(True, 100)], [(200, False)],
    [(200, math.inf)], [(math.nan, 100)], [(200,)], [(200, 100, 50)],
])
def test_normalizer_rejects_malformed(value):
    with pytest.raises(ValueError):
        normalize_closure_observations(value)


def test_normalizer_bound_is_union_not_raw_duplicate_count():
    assert len(normalize_closure_observations([(200, 300)] * 2048)) == 1
    values = [(1000 + i, 100 + i) for i in range(MAX_CLOSURE_OBSERVATIONS)]
    assert len(normalize_closure_observations(values)) == MAX_CLOSURE_OBSERVATIONS
    with pytest.raises(ValueError, match="maximum"):
        normalize_closure_observations(values, valid_to=200, valid_to_recorded_at=500)


@pytest.mark.parametrize("known,expected", [(350, None), (400, 300), (450, 300), (500, 200), (None, 200)])
def test_record_effective_closure(known, expected):
    record = MemoryRecord(
        "mem_review", "example", valid_to=200, valid_to_recorded_at=500,
        closure_observations=((300, 400),),
    )
    assert record.effective_valid_to(known) == expected


def test_legacy_null_observation_is_always_known():
    record = MemoryRecord(
        "mem_review", "example", valid_to=200, valid_to_recorded_at=500,
        closure_observations=((300, None),),
    )
    assert record.effective_valid_to(-1000) == 300


def test_mutated_record_scalar_remains_part_of_union():
    record = MemoryRecord("mem_review", "example", valid_to=300, valid_to_recorded_at=400)
    record.valid_to = 200
    record.valid_to_recorded_at = 500
    assert record.effective_valid_to(450) == 300
    with pytest.raises(ValueError):
        record.effective_valid_to(True)


def test_scoring_uses_previously_known_closure_after_new_correction():
    record = MemoryRecord(
        "mem_review", "example", valid_to=200, valid_to_recorded_at=500,
        closure_observations=((300, 400),),
    )
    weights = scoring.Weights(r=0, s=0, l=0, g=0, i=0, c=0, x=1)
    assert scoring.score_memory(record, now=350, known_at=450, weights=weights) == -1
    assert scoring.score_memory(record, now=350, known_at=350, weights=weights) == 0


def test_schema_version_and_trigger_capture_without_json1():
    assert SCHEMA_VERSION == 19
    conn = database()
    def deny_json(action, left, right, database, source):
        if action == sqlite3.SQLITE_FUNCTION and str(right or left).lower().startswith("json"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(deny_json)
    insert_memory(conn, 300, 400)
    conn.execute("UPDATE memories SET valid_to=200,valid_to_recorded_at=500")
    assert observations(conn) == ((200, 500), (300, 400))
    conn.close()


def test_backfill_legacy_null_seed_and_idempotent_reopen():
    conn = database(install_closures=False)
    insert_memory(conn, 300, None)
    assert observations(conn) == ()
    conn.executescript(MEMORY_CLOSURE_SCHEMA_SQL)
    assert observations(conn) == ((300, None),)
    before = revision(conn)
    conn.executescript(MEMORY_CLOSURE_SCHEMA_SQL)
    assert revision(conn) == before
    assert observations(conn) == ((300, None),)
    conn.close()


def test_null_safe_uniqueness_and_history_only_revision():
    conn = database()
    insert_memory(conn, 200, 500)
    before = revision(conn)
    conn.execute("INSERT INTO memory_closures VALUES ('mem_review',300,NULL)")
    assert revision(conn) != before
    after = revision(conn)
    conn.execute("INSERT INTO memory_closures VALUES ('mem_review',300,NULL) ON CONFLICT DO NOTHING")
    assert revision(conn) == after
    assert observations(conn) == ((200, 500), (300, None))
    conn.close()


def test_capture_rolls_back_with_scalar_write():
    conn = database()
    insert_memory(conn, 300, 400)
    before = revision(conn)
    conn.execute("UPDATE memories SET valid_to=200,valid_to_recorded_at=500")
    conn.rollback()
    assert observations(conn) == ((300, 400),)
    assert conn.execute("SELECT valid_to,valid_to_recorded_at FROM memories").fetchone() == (300, 400)
    assert revision(conn) == before
    conn.close()


def test_cap_rejects_scalar_write_atomically_but_allows_duplicate_replay():
    conn = database()
    insert_memory(conn, 200, 500)
    conn.executemany(
        "INSERT INTO memory_closures VALUES ('mem_review',?,?)",
        [(1000 + i, 100 + i) for i in range(MAX_CLOSURE_OBSERVATIONS - 1)],
    )
    conn.commit()
    before = revision(conn)
    conn.execute("INSERT INTO memory_closures VALUES ('mem_review',200,500) ON CONFLICT DO NOTHING")
    with pytest.raises(sqlite3.IntegrityError, match="observation limit"):
        conn.execute("UPDATE memories SET valid_to=150,valid_to_recorded_at=600")
    assert len(observations(conn)) == MAX_CLOSURE_OBSERVATIONS
    assert conn.execute("SELECT valid_to,valid_to_recorded_at FROM memories").fetchone() == (200, 500)
    assert revision(conn) == before
    conn.close()


@pytest.mark.parametrize("endpoint,recorded", [(math.inf, 100), (200, math.inf), (math.nan, 100)])
def test_sql_rejects_nonfinite_observations(endpoint, recorded):
    conn = database()
    insert_memory(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO memory_closures VALUES ('mem_review',?,?)", (endpoint, recorded))
    conn.close()


def test_invalid_scalar_closure_cannot_silently_lose_trigger_observation():
    conn = database()
    insert_memory(conn, 300, 400)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE memories SET valid_to=?", (math.inf,))
    assert observations(conn) == ((300, 400),)
    assert conn.execute("SELECT valid_to FROM memories").fetchone()[0] == 300
    conn.close()


def test_current_and_historical_visibility_use_covering_index():
    conn = database()
    insert_memory(conn, 300, 400)
    conn.execute("UPDATE memories SET valid_to=200,valid_to_recorded_at=500")
    sql = (
        "SELECT m.id FROM memories m WHERE NOT EXISTS ("
        "SELECT 1 FROM memory_closures c WHERE c.memory_id=m.id AND c.valid_to<=? "
        "AND (c.recorded_at IS NULL OR c.recorded_at<=?))"
    )
    for world, known, visible in ((350, 450, False), (250, 450, True), (250, 550, False), (150, 350, True)):
        assert bool(conn.execute(sql, (world, known)).fetchall()) is visible
    assert any("idx_memory_closures_identity" in row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, (350, 450)))
    conn.close()


def test_memory_erasure_cascades_observations():
    conn = database()
    insert_memory(conn, 300, 400)
    conn.execute("DELETE FROM memories")
    assert observations(conn) == ()
    conn.close()
