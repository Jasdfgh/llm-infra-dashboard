"""Unit tests for ``src.storage.database`` (init_db, get_connection, dict_row_factory)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.storage.database import dict_row_factory, get_connection, init_db


# ── 1. init_db creates 7 tables + FTS5 ───────────────────────────────────


def test_init_db_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_db(db)

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger') ORDER BY type, name"
    ).fetchall()
    conn.close()

    table_names = {name for typ, name in rows if typ == "table"}
    trigger_names = {name for typ, name in rows if typ == "trigger"}

    expected_tables = {
        "signals",
        "signals_fts",
        "signal_comments",
        "signal_changes",
        "signal_refs",
        "sync_runs",
        "etag_cache",
        "signal_labels",
        "signal_gap_ids",
        "signal_stats",
    }
    for t in expected_tables:
        assert t in table_names, f"missing table: {t}"

    expected_triggers = {
        "signals_ai", "signals_au", "signals_ad",
        "signal_stats_ai", "signal_stats_au", "signal_stats_ad",
    }
    for tr in expected_triggers:
        assert tr in trigger_names, f"missing trigger: {tr}"


# ── 2. init_db is idempotent ──────────────────────────────────────────────


def test_init_db_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_db(db)
    init_db(db)


# ── 3. get_connection PRAGMAs ─────────────────────────────────────────────


def test_get_connection_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_db(db)

    conn = get_connection(db)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal == "wal"

        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == 15000
    finally:
        conn.close()


# ── 4. dict_row_factory returns dict ──────────────────────────────────────


def test_dict_row_factory_returns_dict(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_db(db)

    conn = get_connection(db)
    conn.row_factory = dict_row_factory
    try:
        conn.execute(
            "INSERT INTO sync_runs(id, source_type, sync_mode, started_at, status) "
            "VALUES ('run1', 'github', 'full', '2025-01-01', 'completed')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sync_runs WHERE id='run1'").fetchone()
        assert isinstance(row, dict)
        assert row["id"] == "run1"
        assert row["source_type"] == "github"
    finally:
        conn.close()


# ── 5. --force rebuild (delete + recreate) ────────────────────────────────


def test_force_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_db(db)

    conn = get_connection(db)
    conn.execute(
        "INSERT INTO sync_runs(id, source_type, sync_mode, started_at, status) "
        "VALUES ('run1', 'github', 'full', '2025-01-01', 'completed')"
    )
    conn.commit()
    conn.close()

    db.unlink()
    init_db(db)

    conn = get_connection(db)
    conn.row_factory = dict_row_factory
    try:
        rows = conn.execute("SELECT * FROM sync_runs").fetchall()
        assert rows == []
    finally:
        conn.close()


# ── 6. signal_labels auto-population triggers ─────────────────────────

_SIGNAL_INSERT_SQL = (
    "INSERT INTO signals "
    "(signal_id, source_type, source_url, title, body, "
    "created_at, updated_at, first_seen_at, last_synced_at, "
    "content_hash, github_labels) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SIGNAL_COLS = ("http://x", "title", "body", "2026-01-01", "2026-01-01",
                "2026-01-01", "2026-01-01", "abc")


def _insert_signal(conn, sig_id: str, labels_json: str | None, source_type: str = "github_issue") -> None:
    """Helper: insert a minimal signal row with given github_labels JSON."""
    conn.execute(
        _SIGNAL_INSERT_SQL,
        (sig_id, source_type, *_SIGNAL_COLS, labels_json),
    )
    conn.commit()


def _get_labels(conn, sig_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT label FROM signal_labels WHERE signal_id = ? ORDER BY label",
            (sig_id,),
        ).fetchall()
    ]


class TestLabelsTrigger:
    """Tests for signal_labels auto-population triggers."""

    def test_insert_populates_labels(self, tmp_path: Path) -> None:
        """INSERT a signal with github_labels -> signal_labels auto-filled."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal(conn, "sig1", '["rocm","bug","amd"]')
        assert _get_labels(conn, "sig1") == ["amd", "bug", "rocm"]
        conn.close()

    def test_update_rebuilds_labels(self, tmp_path: Path) -> None:
        """UPDATE github_labels -> signal_labels rebuilt."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal(conn, "sig1", '["a","b"]')
        assert _get_labels(conn, "sig1") == ["a", "b"]

        conn.execute(
            "UPDATE signals SET github_labels = ? WHERE signal_id = ?",
            ('["x","y","z"]', "sig1"),
        )
        conn.commit()
        assert _get_labels(conn, "sig1") == ["x", "y", "z"]
        conn.close()

    def test_delete_cleans_labels(self, tmp_path: Path) -> None:
        """DELETE signal -> signal_labels cleaned."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal(conn, "sig1", '["a","b"]')
        assert len(_get_labels(conn, "sig1")) == 2

        conn.execute("DELETE FROM signals WHERE signal_id = ?", ("sig1",))
        conn.commit()
        assert _get_labels(conn, "sig1") == []
        conn.close()

    def test_null_labels_no_trigger(self, tmp_path: Path) -> None:
        """NULL github_labels -> no rows in signal_labels."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal(conn, "sig1", None)
        assert _get_labels(conn, "sig1") == []
        conn.close()

    def test_empty_array_no_trigger(self, tmp_path: Path) -> None:
        """'[]' github_labels -> no rows in signal_labels."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal(conn, "sig1", "[]")
        assert _get_labels(conn, "sig1") == []
        conn.close()


# ── 7. signal_stats trigger tests ──────────────────────────────────────

_STATS_INSERT_SQL = (
    "INSERT INTO signals "
    "(signal_id, source_type, source_url, source_repo, title, body, "
    "created_at, updated_at, first_seen_at, last_synced_at, "
    "content_hash, github_state) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_STATS_COLS = ("title", "body", "2026-01-01", "2026-01-01",
               "2026-01-01", "2026-01-01", "abc")


def _insert_signal_with_state(conn, sig_id: str, source_type: str,
                               source_repo: str, github_state: str | None) -> None:
    conn.execute(
        _STATS_INSERT_SQL,
        (sig_id, source_type, "http://x", source_repo, *_STATS_COLS, github_state),
    )
    conn.commit()


def _get_stats_cnt(conn, source_repo: str, github_state: str) -> int:
    row = conn.execute(
        "SELECT cnt FROM signal_stats WHERE source_repo = ? AND github_state = ?",
        (source_repo, github_state),
    ).fetchone()
    return row[0] if row else 0


class TestSignalStatsTrigger:
    """Tests for signal_stats materialized aggregation triggers."""

    def test_insert_increments_stats(self, tmp_path: Path) -> None:
        """INSERT a signal -> cnt=1 for that (repo, type, state) bucket."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repo", "open")
        assert _get_stats_cnt(conn, "org/repo", "open") == 1
        conn.close()

    def test_insert_multiple_same_bucket(self, tmp_path: Path) -> None:
        """INSERT 3 signals with same (repo, type, state) -> cnt=3."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        for i in range(3):
            _insert_signal_with_state(conn, f"sig{i}", "github_issue", "org/repo", "open")
        assert _get_stats_cnt(conn, "org/repo", "open") == 3
        conn.close()

    def test_insert_different_buckets(self, tmp_path: Path) -> None:
        """INSERT signals with different states -> separate buckets."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repo", "open")
        _insert_signal_with_state(conn, "sig2", "github_issue", "org/repo", "closed")
        assert _get_stats_cnt(conn, "org/repo", "open") == 1
        assert _get_stats_cnt(conn, "org/repo", "closed") == 1
        conn.close()

    def test_delete_decrements_stats(self, tmp_path: Path) -> None:
        """DELETE a signal -> cnt decremented."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repo", "open")
        _insert_signal_with_state(conn, "sig2", "github_issue", "org/repo", "open")
        assert _get_stats_cnt(conn, "org/repo", "open") == 2

        conn.execute("DELETE FROM signals WHERE signal_id = ?", ("sig1",))
        conn.commit()
        assert _get_stats_cnt(conn, "org/repo", "open") == 1
        conn.close()

    def test_update_state_moves_bucket(self, tmp_path: Path) -> None:
        """UPDATE github_state -> old bucket decremented, new bucket incremented."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repo", "open")
        assert _get_stats_cnt(conn, "org/repo", "open") == 1

        conn.execute(
            "UPDATE signals SET github_state = ? WHERE signal_id = ?",
            ("closed", "sig1"),
        )
        conn.commit()
        assert _get_stats_cnt(conn, "org/repo", "open") == 0
        assert _get_stats_cnt(conn, "org/repo", "closed") == 1
        conn.close()

    def test_null_state_coalesced(self, tmp_path: Path) -> None:
        """INSERT with github_state=None -> bucket uses '' as key."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repo", None)
        assert _get_stats_cnt(conn, "org/repo", "") == 1
        conn.close()

    def test_update_changes_stats(self, tmp_path: Path) -> None:
        """UPDATE repo/state -> old bucket decremented, new bucket incremented across dimensions."""
        db = tmp_path / "test.db"
        init_db(db)
        conn = get_connection(db)

        # Step 1: insert signal (repo=A, state=open, type=github_issue)
        _insert_signal_with_state(conn, "sig1", "github_issue", "org/repoA", "open")
        assert _get_stats_cnt(conn, "org/repoA", "open") == 1

        # Step 2: update state open -> closed
        conn.execute(
            "UPDATE signals SET github_state = ? WHERE signal_id = ?",
            ("closed", "sig1"),
        )
        conn.commit()
        assert _get_stats_cnt(conn, "org/repoA", "open") == 0
        assert _get_stats_cnt(conn, "org/repoA", "closed") == 1

        # Step 3: update repo A -> B (state remains closed)
        conn.execute(
            "UPDATE signals SET source_repo = ? WHERE signal_id = ?",
            ("org/repoB", "sig1"),
        )
        conn.commit()
        assert _get_stats_cnt(conn, "org/repoA", "closed") == 0
        assert _get_stats_cnt(conn, "org/repoB", "closed") == 1

        conn.close()

    def test_backfill_populates_stats(self, tmp_path: Path) -> None:
        """Backfill: insert raw rows without triggers, then init_db populates stats."""
        db = tmp_path / "test.db"
        # Create schema without signal_stats triggers by using raw DDL
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE signals (
                rowid INTEGER PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_repo TEXT,
                source_number INTEGER,
                title TEXT NOT NULL,
                body TEXT,
                body_token_estimate INTEGER DEFAULT 0,
                author TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_synced_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                sync_run_id TEXT,
                references_json TEXT,
                tags TEXT,
                github_json TEXT,
                github_state TEXT,
                github_labels TEXT,
                github_is_pr INTEGER DEFAULT 0,
                github_comment_count INTEGER DEFAULT 0,
                twitter_json TEXT,
                arxiv_json TEXT,
                classification_json TEXT,
                gap_ids TEXT
            )
        """)
        # Insert raw rows (no triggers active)
        for i in range(5):
            conn.execute(
                _STATS_INSERT_SQL,
                (f"sig{i}", "github_issue", "http://x", "org/repo", *_STATS_COLS, "open"),
            )
        for i in range(5, 8):
            conn.execute(
                _STATS_INSERT_SQL,
                (f"sig{i}", "github_pr", "http://x", "org/repo", *_STATS_COLS, "closed"),
            )
        conn.commit()
        conn.close()

        # Now run init_db which will apply full DDL + backfill
        init_db(db)

        conn = get_connection(db)
        assert _get_stats_cnt(conn, "org/repo", "open") == 5
        assert _get_stats_cnt(conn, "org/repo", "closed") == 3
        conn.close()
