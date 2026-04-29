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
    }
    for t in expected_tables:
        assert t in table_names, f"missing table: {t}"

    expected_triggers = {"signals_ai", "signals_au", "signals_ad"}
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
        assert busy == 5000
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
