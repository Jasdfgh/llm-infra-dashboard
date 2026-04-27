"""SQLite connection + DDL initialization (Layer 1-A of Module 3).

This module is deliberately thin: it only opens connections with the right
PRAGMAs and applies the schema idempotently. All CRUD / query logic belongs
to Layer 2 (`repository.py`) and MUST NOT leak back in here.

The DDL below mirrors `design/module_1_3_architecture.md` D2.2 column-for-
column. If the design doc changes, update `_DDL_STATEMENTS` first and keep
the two in lockstep.

See D2.2 (SQLite DDL) and D8 (MVP 4/24 deliverables).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH: Path = Path("data/signals.db")


# DDL executed sequentially by `init_db`. Every statement is guarded with
# IF NOT EXISTS so re-running against an existing DB is a no-op. Order is
# significant: the FTS5 virtual table and its triggers must come after the
# `signals` base table (content=signals references it).
_DDL_STATEMENTS: tuple[str, ...] = (
    # ── signals: main table ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS signals (
        rowid                 INTEGER PRIMARY KEY,
        signal_id             TEXT    NOT NULL UNIQUE,

        source_type           TEXT    NOT NULL,
        source_url            TEXT    NOT NULL,
        source_repo           TEXT,
        source_number         INTEGER,

        title                 TEXT    NOT NULL,
        body                  TEXT,
        body_token_estimate   INTEGER DEFAULT 0,
        author                TEXT,
        created_at            TEXT    NOT NULL,
        updated_at            TEXT    NOT NULL,

        first_seen_at         TEXT    NOT NULL,
        last_synced_at        TEXT    NOT NULL,
        content_hash          TEXT    NOT NULL,
        version               INTEGER DEFAULT 1,
        sync_run_id           TEXT,

        references_json       TEXT,

        tags                  TEXT,

        github_json           TEXT,
        github_state          TEXT,
        github_labels         TEXT,
        github_is_pr          INTEGER DEFAULT 0,
        github_comment_count  INTEGER DEFAULT 0,

        twitter_json          TEXT,

        arxiv_json            TEXT,

        classification_json   TEXT,
        gap_ids               TEXT
    )
    """,
    # Deterministic indexes (D2.2 §"四种确定性索引" + dedup unique index).
    "CREATE INDEX IF NOT EXISTS idx_signals_repo_updated ON signals(source_repo, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals_synced ON signals(last_synced_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals_type_state ON signals(source_type, github_state)",
    # Partial index: Module 2 consumes the unclassified feed in FIFO order.
    """
    CREATE INDEX IF NOT EXISTS idx_signals_feed
        ON signals(last_synced_at ASC)
        WHERE classification_json IS NULL
    """,
    # Partial unique index: GitHub dedup by (repo, number); skips non-GitHub
    # rows where source_number IS NULL (tweets, blog posts, ...).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup
        ON signals(source_type, source_repo, source_number)
        WHERE source_number IS NOT NULL
    """,
    # ── signals_fts: FTS5 virtual table (contentless, backed by signals) ──
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS signals_fts USING fts5(
        title,
        body,
        tags,
        content=signals,
        content_rowid=rowid,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    # content=signals means we must explicitly keep the FTS5 index in sync
    # via triggers. The 'delete'+insert pattern on update is the canonical
    # SQLite FTS5 contentless-table recipe.
    """
    CREATE TRIGGER IF NOT EXISTS signals_ai AFTER INSERT ON signals BEGIN
        INSERT INTO signals_fts(rowid, title, body, tags)
        VALUES (new.rowid, new.title, new.body, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS signals_au AFTER UPDATE ON signals BEGIN
        INSERT INTO signals_fts(signals_fts, rowid, title, body, tags)
        VALUES ('delete', old.rowid, old.title, old.body, old.tags);
        INSERT INTO signals_fts(rowid, title, body, tags)
        VALUES (new.rowid, new.title, new.body, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS signals_ad AFTER DELETE ON signals BEGIN
        INSERT INTO signals_fts(signals_fts, rowid, title, body, tags)
        VALUES ('delete', old.rowid, old.title, old.body, old.tags);
    END
    """,
    # ── signal_comments ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS signal_comments (
        id                   INTEGER PRIMARY KEY,
        signal_id            TEXT    NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
        comment_id           TEXT    NOT NULL,
        author               TEXT,
        body                 TEXT,
        body_token_estimate  INTEGER DEFAULT 0,
        created_at           TEXT    NOT NULL,
        updated_at           TEXT,
        is_bot               INTEGER DEFAULT 0,

        UNIQUE(signal_id, comment_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comments_signal ON signal_comments(signal_id, created_at ASC)",
    # ── signal_changes: append-only audit log ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS signal_changes (
        id             INTEGER PRIMARY KEY,
        signal_id      TEXT    NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
        change_type    TEXT    NOT NULL,
        changed_at     TEXT    NOT NULL,
        detected_at    TEXT    NOT NULL,
        old_value      TEXT,
        new_value      TEXT,
        is_meaningful  INTEGER DEFAULT 1,
        sync_run_id    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_changes_signal ON signal_changes(signal_id, detected_at DESC)",
    # Partial index: dashboards/feeds usually want meaningful changes only;
    # bot noise (is_meaningful=0) would otherwise dominate.
    """
    CREATE INDEX IF NOT EXISTS idx_changes_meaningful
        ON signal_changes(detected_at DESC)
        WHERE is_meaningful = 1
    """,
    # ── signal_refs: cross-signal references ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS signal_refs (
        id              INTEGER PRIMARY KEY,
        from_signal_id  TEXT    NOT NULL,
        to_signal_id    TEXT,
        to_url          TEXT    NOT NULL,
        ref_type        TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,

        UNIQUE(from_signal_id, to_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_refs_from ON signal_refs(from_signal_id)",
    # Partial index: reverse lookups only make sense once target is resolved.
    """
    CREATE INDEX IF NOT EXISTS idx_refs_to
        ON signal_refs(to_signal_id)
        WHERE to_signal_id IS NOT NULL
    """,
    # ── sync_runs ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id                TEXT    PRIMARY KEY,

        source_type       TEXT    NOT NULL,
        source_repo       TEXT,
        sync_mode         TEXT    NOT NULL,
        started_at        TEXT    NOT NULL,
        completed_at      TEXT,
        status            TEXT    NOT NULL,

        signals_total     INTEGER DEFAULT 0,
        signals_created   INTEGER DEFAULT 0,
        signals_updated   INTEGER DEFAULT 0,
        signals_unchanged INTEGER DEFAULT 0,
        comments_fetched  INTEGER DEFAULT 0,
        api_calls_used    INTEGER DEFAULT 0,

        error_message     TEXT,
        resume_token      TEXT,

        config_snapshot   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_repo ON sync_runs(source_repo, started_at DESC)",
    # ── etag_cache: HTTP conditional-request cache ───────────────────────
    """
    CREATE TABLE IF NOT EXISTS etag_cache (
        url            TEXT    PRIMARY KEY,
        etag           TEXT,
        last_modified  TEXT,
        cached_at      TEXT    NOT NULL
    )
    """,
)


def dict_row_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    """Row factory that returns each row as a plain ``dict``.

    Attach with ``conn.row_factory = dict_row_factory`` before executing
    queries so repository callers can address columns by name instead of
    positional index.
    """
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with project PRAGMA defaults.

    Applies on every new connection:

    * ``journal_mode = WAL``      — concurrent readers + single writer.
    * ``foreign_keys = ON``       — enforce FK constraints (off by default).
    * ``busy_timeout = 5000`` ms  — wait up to 5 s for a lock before SQLITE_BUSY.

    The caller owns the returned connection and is responsible for closing
    it (or using a ``with`` block). The parent directory is NOT auto-created
    here — use :func:`init_db` or create it beforehand.

    See D2.2 (PRAGMA block).
    """
    path = Path(db_path)
    conn = sqlite3.connect(str(path))
    # journal_mode persists in the DB header; the others are per-connection
    # and so must be re-applied on each open.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create or upgrade the schema at ``db_path``.

    Idempotent: every statement uses ``IF NOT EXISTS``, so running against
    an existing database is a no-op. This function never drops or migrates
    data — destructive resets are the caller's responsibility (see the
    ``--force`` flag in ``scripts/init_db.py``).

    The parent directory is created automatically.

    See D2.2 (full SQLite DDL).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    try:
        with conn:
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
    finally:
        conn.close()
