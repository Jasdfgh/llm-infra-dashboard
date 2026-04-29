#!/usr/bin/env python3
"""Performance regression tests against the real signals.db.

Skipped when the DB doesn't exist (CI without data).
Run with: .venv/bin/python -m pytest tests/test_performance.py -v
"""
import os
import time
import sqlite3
from pathlib import Path

import pytest

DB_PATH = Path("data/signals.db")

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists(),
    reason="Real signals.db not available",
)


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_count(c: sqlite3.Connection, name: str) -> int:
    return c.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA busy_timeout = 10000")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA query_only = ON")
    yield c
    c.close()


def _has_denorm_table(conn, table: str) -> bool:
    """Check if a denormalized helper table exists and is populated."""
    if not _table_exists(conn, table):
        return False
    return _table_count(conn, table) > 0


def _time_query(conn, sql, params=(), iterations=5):
    """Run query multiple times, return median ms."""
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        conn.execute(sql, params).fetchall()
        times.append((time.perf_counter_ns() - t0) / 1e6)
    times.sort()
    return times[len(times) // 2]


# -- Tests that require signal_labels / signal_gap_ids tables ----------------

def test_perf_labels_filter(conn):
    """EXISTS + signal_labels lookup must be < 50ms (pre-opt: 983ms)."""
    if not _has_denorm_table(conn, "signal_labels"):
        pytest.skip("signal_labels table not populated (run init_db + backfill)")

    sql = (
        "SELECT COUNT(*) FROM signals s "
        "WHERE EXISTS ("
        "  SELECT 1 FROM signal_labels sl "
        "  WHERE sl.signal_id = s.signal_id AND sl.label = 'rocm'"
        ")"
    )
    median_ms = _time_query(conn, sql)
    assert median_ms < 50, f"labels filter took {median_ms:.1f}ms, want < 50ms"


def test_perf_gap_ids_filter(conn):
    """EXISTS + signal_gap_ids lookup must be < 50ms (pre-opt: 476ms)."""
    if not _has_denorm_table(conn, "signal_gap_ids"):
        pytest.skip("signal_gap_ids table not populated (run init_db + backfill)")

    sql = (
        "SELECT COUNT(*) FROM signals s "
        "WHERE EXISTS ("
        "  SELECT 1 FROM signal_gap_ids sg "
        "  WHERE sg.signal_id = s.signal_id AND sg.gap_id = 'GAP-001'"
        ")"
    )
    median_ms = _time_query(conn, sql)
    assert median_ms < 50, f"gap_ids filter took {median_ms:.1f}ms, want < 50ms"


# -- Tests that work on the existing schema ----------------------------------

def test_perf_repo_state(conn):
    """Indexed (source_repo, github_state) scan must be < 100ms (pre-opt: 732ms)."""
    repo = conn.execute(
        "SELECT source_repo FROM signals GROUP BY source_repo "
        "ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    sql = (
        "SELECT COUNT(*) FROM signals "
        "WHERE source_repo = ? AND github_state = ?"
    )
    median_ms = _time_query(conn, sql, (repo, "open"))
    assert median_ms < 100, f"repo+state filter took {median_ms:.1f}ms, want < 100ms"


def test_perf_fts_search(conn):
    """FTS5 MATCH search must be < 10ms."""
    sql = (
        "SELECT COUNT(*) FROM signals_fts WHERE signals_fts MATCH 'ROCm'"
    )
    median_ms = _time_query(conn, sql)
    assert median_ms < 10, f"FTS search took {median_ms:.1f}ms, want < 10ms"


def test_perf_get_by_id(conn):
    """Primary-key lookup must be < 1ms."""
    sid = conn.execute("SELECT signal_id FROM signals LIMIT 1").fetchone()[0]
    sql = "SELECT * FROM signals WHERE signal_id = ?"
    median_ms = _time_query(conn, sql, (sid,))
    assert median_ms < 1, f"get_by_id took {median_ms:.1f}ms, want < 1ms"


def test_perf_group_by_repo(conn):
    """GROUP BY source_repo aggregation must be < 100ms."""
    sql = (
        "SELECT source_repo, COUNT(*) AS cnt FROM signals "
        "GROUP BY source_repo ORDER BY cnt DESC"
    )
    median_ms = _time_query(conn, sql)
    assert median_ms < 100, f"group_by_repo took {median_ms:.1f}ms, want < 100ms"


def test_perf_feed_unclassified(conn):
    """Unclassified feed query (partial index) must be < 50ms."""
    sql = (
        "SELECT * FROM signals "
        "WHERE classification_json IS NULL AND last_synced_at >= '2026-01-01T00:00:00Z' "
        "ORDER BY last_synced_at ASC, signal_id ASC LIMIT 100"
    )
    median_ms = _time_query(conn, sql)
    assert median_ms < 50, f"feed_unclassified took {median_ms:.1f}ms, want < 50ms"


def test_perf_count_total(conn):
    """Simple COUNT(*) must be < 20ms."""
    sql = "SELECT COUNT(*) FROM signals"
    median_ms = _time_query(conn, sql)
    assert median_ms < 20, f"count_total took {median_ms:.1f}ms, want < 20ms"
