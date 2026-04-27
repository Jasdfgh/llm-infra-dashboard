"""SignalRepository — Layer 2-E CRUD over signals.db.

This is the thin persistence layer for Module 1+3. It speaks only Pydantic
models (see `src.ingestion.models`) and SQLite rows; higher layers
(Orchestrator, Search, API) compose on top.

Key design decisions (see `design/module_1_3_architecture.md`):

* **Per-signal transactions** (D4 Step 7): each write method wraps its SQL
  in ``with self._conn:`` so a single failure doesn't abort a whole batch.
  The orchestrator iterates signals and catches exceptions per signal.

* **Explicit INSERT / UPDATE** (not ``INSERT OR REPLACE``) for `signals`.
  ``INSERT OR REPLACE`` would delete the old row first, cascading through
  ``ON DELETE CASCADE`` and wiping `signal_comments` + `signal_changes`.
  We SELECT first to decide new-vs-update, then dispatch accordingly.

* **JSON fields stay as strings** on the way out. ``get_by_id`` returns
  TEXT columns verbatim; the caller (typically `SignalSearch`) does
  ``json.loads`` as needed. This keeps the repo thin and avoids hidden
  parse costs.

* **Connection ownership**: either open our own via ``get_connection`` or
  accept an injected ``sqlite3.Connection``. Injected connections are NOT
  closed by ``close()`` and do NOT auto-commit per call — the caller owns
  the transaction lifecycle.

* **Version policy** (see `upsert_signal`):
    - new insert → version = 1 (unless ``version_override`` is set)
    - update, no override → version = existing + 1
    - update, override set → use override verbatim
  The orchestrator uses ``version_override = existing`` when
  ``content_hash`` is unchanged (re-sync of unchanged content bumps
  ``last_synced_at`` but NOT version).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from src.ingestion.models import (
    ChangeEvent,
    Comment,
    ResumeToken,
    Signal,
    SignalRef,
    SyncRun,
    SyncStatus,
)
from src.storage.database import DEFAULT_DB_PATH, dict_row_factory, get_connection


# ───────────────────────── helpers ─────────────────────────


def _enum_to_str(value: Any) -> Any:
    """Return ``value.value`` if it's an Enum, else ``value``.

    Defensive helper — Signal/SyncRun use ``use_enum_values=True`` so
    attributes are already strings, but update_sync_run can receive an
    enum member directly from the caller (e.g. ``SyncStatus.COMPLETED``).
    """
    return getattr(value, "value", value)


def _dumps(obj: Any) -> Optional[str]:
    """JSON-encode ``obj`` or return None if obj is None.

    ``ensure_ascii=False`` keeps CJK characters readable when the DB is
    inspected manually.
    """
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


# ─────────────────────── Repository ────────────────────────


# Columns written to `signals` on INSERT / UPDATE (order doesn't matter
# for named-param binding; kept explicit for review clarity).
_SIGNAL_COLUMNS: tuple[str, ...] = (
    "signal_id",
    "source_type",
    "source_url",
    "source_repo",
    "source_number",
    "title",
    "body",
    "body_token_estimate",
    "author",
    "created_at",
    "updated_at",
    "first_seen_at",
    "last_synced_at",
    "content_hash",
    "version",
    "sync_run_id",
    "references_json",
    "tags",
    "github_json",
    "github_state",
    "github_labels",
    "github_is_pr",
    "github_comment_count",
    "twitter_json",
    "arxiv_json",
    "classification_json",
    "gap_ids",
)


class SignalRepository:
    """CRUD layer for signals.db. See D2.2 and D4 Step 7.

    Usage (owned connection)::

        with SignalRepository("data/signals.db") as repo:
            repo.upsert_signal(sig)
            repo.upsert_comments(comments)

    Usage (injected connection — caller controls transaction)::

        conn = get_connection("data/signals.db")
        conn.row_factory = dict_row_factory
        try:
            with conn:  # outer atomic batch
                repo = SignalRepository(connection=conn)
                repo.upsert_signal(sig)
                repo.upsert_comments(cs)
        finally:
            conn.close()
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Open (or adopt) a connection.

        If ``connection`` is injected, we do NOT take ownership and do NOT
        auto-commit in each write method — the caller is responsible for
        transaction boundaries. If ``db_path`` is used, we open via
        ``get_connection`` (applies project PRAGMAs) and wrap each write
        in its own ``with conn:`` block.
        """
        if connection is not None:
            self._conn: sqlite3.Connection = connection
            self._owns_conn: bool = False
            if connection.row_factory is not dict_row_factory:
                connection.row_factory = dict_row_factory
        else:
            self._conn = get_connection(db_path)
            self._conn.row_factory = dict_row_factory
            self._owns_conn = True
        self._in_transaction: bool = False

    # ── Lifecycle ─────────────────────────────────────────

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the underlying connection (e.g. for ``SignalSearch``)."""
        return self._conn

    def close(self) -> None:
        """Close the connection only if we own it.

        Idempotent: safe to call multiple times.
        """
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            finally:
                # `None`-out so double-close is a no-op.
                self._conn = None  # type: ignore[assignment]
                self._owns_conn = False

    def __enter__(self) -> "SignalRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _txn(self):
        """Internal transaction context manager.

        - If an outer ``transaction()`` is active → no-op yield (let the
          outer context handle commit/rollback).
        - Owned connection, no outer txn → ``with self._conn:`` auto-commit.
        - Injected connection, no outer txn → yield only; caller manages.
        """
        if self._in_transaction:
            yield self._conn
        elif self._owns_conn:
            with self._conn:
                yield self._conn
        else:
            yield self._conn

    @contextmanager
    def transaction(self):
        """Public transaction context manager for multi-method atomicity.

        Use when several write methods must commit together as one unit
        (e.g. upsert_signal + upsert_comments + append_changes + upsert_refs
        for a single signal — the D4 Step 7 "每条 signal 独立事务" contract).

        While active, individual write methods' ``_txn()`` becomes a no-op
        so that only this outer ``with self._conn:`` controls commit/rollback.
        """
        if self._in_transaction:
            raise RuntimeError("transaction() is not re-entrant")
        self._in_transaction = True
        try:
            with self._conn:
                yield self._conn
        finally:
            self._in_transaction = False

    # ── Signal CRUD ────────────────────────────────────────

    def get_by_id(self, signal_id: str) -> Optional[dict[str, Any]]:
        """Fetch one row by signal_id. Returns None if not found.

        JSON columns (``references_json``, ``tags``, ``github_labels``, …)
        are returned as raw TEXT — caller parses.
        """
        cur = self._conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?",
            (signal_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def exists(self, signal_id: str) -> bool:
        """Check presence without fetching the full row."""
        cur = self._conn.execute(
            "SELECT 1 FROM signals WHERE signal_id = ? LIMIT 1",
            (signal_id,),
        )
        return cur.fetchone() is not None

    def _signal_to_row(self, signal: Signal, version: int) -> dict[str, Any]:
        """Map a Signal into a row dict matching ``_SIGNAL_COLUMNS``.

        JSON-encodes all dict/list fields and fills the redundant GitHub
        physical columns (``github_state``, ``github_labels``,
        ``github_is_pr``, ``github_comment_count``) for index-backed
        filters (see D2.2 comment on "冗余物理列").
        """
        gh = signal.github
        tw = signal.twitter
        ar = signal.arxiv
        cls_ = signal.classification

        return {
            "signal_id": signal.signal_id,
            "source_type": _enum_to_str(signal.source_type),
            "source_url": signal.source_url,
            "source_repo": signal.source_repo,
            "source_number": signal.source_number,
            "title": signal.title,
            "body": signal.body,
            "body_token_estimate": signal.body_token_estimate,
            "author": signal.author,
            "created_at": signal.created_at,
            "updated_at": signal.updated_at,
            "first_seen_at": signal.first_seen_at,
            "last_synced_at": signal.last_synced_at,
            "content_hash": signal.content_hash,
            "version": version,
            "sync_run_id": signal.sync_run_id,
            "references_json": (
                _dumps(signal.references.model_dump(mode="json"))
                if signal.references is not None
                else None
            ),
            "tags": _dumps(list(signal.tags)) if signal.tags is not None else None,
            # GitHub: JSON blob + redundant physical columns.
            "github_json": _dumps(gh.model_dump(mode="json")) if gh else None,
            "github_state": (_enum_to_str(gh.state) if gh else None),
            "github_labels": (
                _dumps(list(gh.labels)) if gh and gh.labels is not None else None
            ),
            "github_is_pr": (1 if gh and gh.is_pr else 0),
            "github_comment_count": (gh.comment_count if gh else 0),
            # Other source payloads.
            "twitter_json": _dumps(tw.model_dump(mode="json")) if tw else None,
            "arxiv_json": _dumps(ar.model_dump(mode="json")) if ar else None,
            # Classification (written back by Module 2; usually None here).
            "classification_json": (
                _dumps(cls_.model_dump(mode="json")) if cls_ else None
            ),
            "gap_ids": (_dumps(list(cls_.gap_ids)) if cls_ else None),
        }

    def upsert_signal(
        self,
        signal: Signal,
        *,
        version_override: int | None = None,
    ) -> tuple[bool, int]:
        """Insert or update a signal. Returns ``(is_new, version_written)``.

        Strategy (explicit INSERT / UPDATE — see module docstring for why
        we don't use ``INSERT OR REPLACE``):

        1. SELECT existing row to decide new-vs-update and capture its
           current version.
        2. New → INSERT with version = ``version_override or 1``.
        3. Existing, no override → UPDATE with version = old + 1.
        4. Existing, override set → UPDATE with version = override (used
           when orchestrator detects no meaningful change — just bumps
           ``last_synced_at`` without version change).

        Each call wraps its own transaction (unless connection is
        injected, in which case the caller's transaction applies).
        """
        existing = self.get_by_id(signal.signal_id)
        is_new = existing is None

        if is_new:
            version = version_override if version_override is not None else 1
        else:
            if version_override is not None:
                version = version_override
            else:
                prev = int(existing.get("version") or 0)  # type: ignore[union-attr]
                version = prev + 1

        row = self._signal_to_row(signal, version)

        with self._txn():
            if is_new:
                placeholders = ", ".join("?" for _ in _SIGNAL_COLUMNS)
                sql = (
                    f"INSERT INTO signals ({', '.join(_SIGNAL_COLUMNS)}) "
                    f"VALUES ({placeholders})"
                )
                self._conn.execute(sql, [row[c] for c in _SIGNAL_COLUMNS])
            else:
                # Don't touch signal_id in the SET list; it's the WHERE key.
                set_cols = [c for c in _SIGNAL_COLUMNS if c != "signal_id"]
                set_clause = ", ".join(f"{c} = ?" for c in set_cols)
                sql = f"UPDATE signals SET {set_clause} WHERE signal_id = ?"
                self._conn.execute(
                    sql,
                    [row[c] for c in set_cols] + [signal.signal_id],
                )

        return is_new, version

    # ── Comments ───────────────────────────────────────────

    def upsert_comments(self, comments: Iterable[Comment]) -> int:
        """INSERT OR IGNORE into ``signal_comments``. Returns inserted count.

        Dedup is enforced by ``UNIQUE(signal_id, comment_id)`` — re-syncing
        the same comment is a no-op. Foreign key ``signal_id REFERENCES
        signals(signal_id) ON DELETE CASCADE`` requires the parent signal
        row to exist first (orchestrator calls ``upsert_signal`` before
        this).
        """
        comments = list(comments)
        if not comments:
            return 0

        inserted = 0
        with self._txn():
            for c in comments:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_comments
                        (signal_id, comment_id, author, body,
                         body_token_estimate, created_at, updated_at, is_bot)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        c.signal_id,
                        c.comment_id,
                        c.author,
                        c.body,
                        c.body_token_estimate,
                        c.created_at,
                        c.updated_at,
                        1 if c.is_bot else 0,
                    ),
                )
                # rowcount is 1 on insert, 0 on IGNORE (sqlite3 semantics).
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
        return inserted

    def get_comments(
        self,
        signal_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch comments for a signal, ordered by created_at ASC.

        ``limit=None`` returns all.
        """
        sql = (
            "SELECT * FROM signal_comments "
            "WHERE signal_id = ? ORDER BY created_at ASC"
        )
        params: list[Any] = [signal_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ── Change events ──────────────────────────────────────

    def append_changes(self, changes: Iterable[ChangeEvent]) -> int:
        """Append-only insert into ``signal_changes``. Returns count.

        Never updates existing rows (audit log). ``old_value`` and
        ``new_value`` are already JSON strings in the model (see D2.3),
        so we write them verbatim.
        """
        changes = list(changes)
        if not changes:
            return 0

        inserted = 0
        with self._txn():
            for ev in changes:
                cur = self._conn.execute(
                    """
                    INSERT INTO signal_changes
                        (signal_id, change_type, changed_at, detected_at,
                         old_value, new_value, is_meaningful, sync_run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev.signal_id,
                        _enum_to_str(ev.change_type),
                        ev.changed_at,
                        ev.detected_at,
                        ev.old_value,
                        ev.new_value,
                        1 if ev.is_meaningful else 0,
                        ev.sync_run_id,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
        return inserted

    def get_changes(
        self,
        signal_id: str | None = None,
        *,
        since: str | None = None,
        meaningful_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query change events, ordered ``detected_at DESC``.

        Args:
            signal_id: filter to one signal, or None for all.
            since: ISO timestamp — only events with ``detected_at >= since``.
            meaningful_only: exclude bot-noise events (``is_meaningful=0``).
            limit: cap rows returned.
        """
        where: list[str] = []
        params: list[Any] = []

        if signal_id is not None:
            where.append("signal_id = ?")
            params.append(signal_id)
        if since is not None:
            where.append("detected_at >= ?")
            params.append(since)
        if meaningful_only:
            where.append("is_meaningful = 1")

        where_clause = f" WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT * FROM signal_changes{where_clause} "
            f"ORDER BY detected_at DESC LIMIT ?"
        )
        params.append(limit)

        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ── Signal refs ────────────────────────────────────────

    def upsert_refs(self, refs: Iterable[SignalRef]) -> int:
        """INSERT OR IGNORE into ``signal_refs``. Returns inserted count.

        Dedup by ``UNIQUE(from_signal_id, to_url)``. Note: ``signal_refs``
        does NOT have a foreign key to ``signals`` (unlike comments /
        changes) — by design, we may record a ref to a target that hasn't
        been ingested yet (``to_signal_id = NULL``), and fill it in later
        via ``reconcile_refs`` (D4 Step 7e).
        """
        refs = list(refs)
        if not refs:
            return 0

        inserted = 0
        with self._txn():
            for r in refs:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_refs
                        (from_signal_id, to_signal_id, to_url, ref_type, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        r.from_signal_id,
                        r.to_signal_id,
                        r.to_url,
                        _enum_to_str(r.ref_type),
                        r.created_at,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
        return inserted

    def reconcile_refs(self) -> int:
        """Resolve ``signal_refs.to_signal_id`` by URL match. Returns count
        of rows updated.

        Called at the end of each sync run (D4 Step 7e). Matches
        ``signal_refs.to_url`` against ``signals.source_url`` and fills in
        the corresponding ``to_signal_id`` for all previously unresolved
        refs.
        """
        with self._txn():
            cur = self._conn.execute(
                """
                UPDATE signal_refs
                   SET to_signal_id = (
                       SELECT s.signal_id FROM signals s
                       WHERE s.source_url = signal_refs.to_url
                       LIMIT 1
                   )
                 WHERE to_signal_id IS NULL
                   AND EXISTS (
                       SELECT 1 FROM signals s
                       WHERE s.source_url = signal_refs.to_url
                   )
                """
            )
            return cur.rowcount or 0

    # ── Sync runs ──────────────────────────────────────────

    def create_sync_run(self, run: SyncRun) -> None:
        """Insert a new sync_runs row. Caller generates the ``id`` (TEXT PK).

        Typically status='running' at this point; ``update_sync_run``
        flips to 'completed'/'partial'/'failed' at the end.
        """
        resume = (
            _dumps(run.resume_token.model_dump(mode="json"))
            if run.resume_token is not None
            else None
        )
        config = _dumps(run.config_snapshot) if run.config_snapshot else None

        with self._txn():
            self._conn.execute(
                """
                INSERT INTO sync_runs
                    (id, source_type, source_repo, sync_mode, started_at,
                     completed_at, status,
                     signals_total, signals_created, signals_updated,
                     signals_unchanged, comments_fetched, api_calls_used,
                     error_message, resume_token, config_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    _enum_to_str(run.source_type),
                    run.source_repo,
                    _enum_to_str(run.sync_mode),
                    run.started_at,
                    run.completed_at,
                    _enum_to_str(run.status),
                    run.signals_total,
                    run.signals_created,
                    run.signals_updated,
                    run.signals_unchanged,
                    run.comments_fetched,
                    run.api_calls_used,
                    run.error_message,
                    resume,
                    config,
                ),
            )

    def update_sync_run(
        self,
        run_id: str,
        *,
        status: SyncStatus | str | None = None,
        completed_at: str | None = None,
        signals_total: int | None = None,
        signals_created: int | None = None,
        signals_updated: int | None = None,
        signals_unchanged: int | None = None,
        comments_fetched: int | None = None,
        api_calls_used: int | None = None,
        error_message: str | None = None,
        resume_token: ResumeToken | None = None,
    ) -> None:
        """Partial update of a sync_runs row. None = don't change.

        Typical flow: create_sync_run(status='running') → … →
        update_sync_run(status='completed', completed_at=now, <stats>).
        """
        set_parts: list[str] = []
        params: list[Any] = []

        def _add(col: str, value: Any) -> None:
            set_parts.append(f"{col} = ?")
            params.append(value)

        if status is not None:
            _add("status", _enum_to_str(status))
        if completed_at is not None:
            _add("completed_at", completed_at)
        if signals_total is not None:
            _add("signals_total", signals_total)
        if signals_created is not None:
            _add("signals_created", signals_created)
        if signals_updated is not None:
            _add("signals_updated", signals_updated)
        if signals_unchanged is not None:
            _add("signals_unchanged", signals_unchanged)
        if comments_fetched is not None:
            _add("comments_fetched", comments_fetched)
        if api_calls_used is not None:
            _add("api_calls_used", api_calls_used)
        if error_message is not None:
            _add("error_message", error_message)
        if resume_token is not None:
            _add("resume_token", _dumps(resume_token.model_dump(mode="json")))

        if not set_parts:
            return  # nothing to change

        sql = f"UPDATE sync_runs SET {', '.join(set_parts)} WHERE id = ?"
        params.append(run_id)

        with self._txn():
            self._conn.execute(sql, params)

    def get_last_successful_sync(
        self, source_repo: str
    ) -> Optional[dict[str, Any]]:
        """Most recent ``status='completed'`` sync for ``source_repo``.

        Used by the orchestrator to compute ``since`` for incremental
        pulls (D3.4). Returns None if no completed run exists.
        """
        cur = self._conn.execute(
            """
            SELECT * FROM sync_runs
             WHERE source_repo = ? AND status = 'completed'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (source_repo,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── ETag cache ─────────────────────────────────────────

    def get_etag(self, url: str) -> Optional[dict[str, Any]]:
        """Return the cached ETag/Last-Modified entry for ``url`` or None."""
        cur = self._conn.execute(
            "SELECT * FROM etag_cache WHERE url = ?",
            (url,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def save_etag(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Upsert an ETag cache entry. ``cached_at`` is set to now() UTC."""
        from datetime import datetime, timezone

        cached_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._txn():
            self._conn.execute(
                """
                INSERT INTO etag_cache (url, etag, last_modified, cached_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    cached_at = excluded.cached_at
                """,
                (url, etag, last_modified, cached_at),
            )

    # ── Stats ──────────────────────────────────────────────

    def count_signals(
        self,
        *,
        source_repo: str | None = None,
        state: str | None = None,
    ) -> int:
        """Count signals with optional filters.

        ``state`` filters on ``github_state`` (the redundant physical
        column — see D2.2).
        """
        where: list[str] = []
        params: list[Any] = []

        if source_repo is not None:
            where.append("source_repo = ?")
            params.append(source_repo)
        if state is not None:
            where.append("github_state = ?")
            params.append(state)

        where_clause = f" WHERE {' AND '.join(where)}" if where else ""
        cur = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM signals{where_clause}",
            params,
        )
        return int(cur.fetchone()["n"])

    def count_changes(
        self,
        *,
        since: str | None = None,
        meaningful_only: bool = True,
    ) -> int:
        """Count change-log rows with optional ``detected_at >= since``
        and meaningful-only filter (default True, matches D2.2 partial
        index ``idx_changes_meaningful``).
        """
        where: list[str] = []
        params: list[Any] = []

        if since is not None:
            where.append("detected_at >= ?")
            params.append(since)
        if meaningful_only:
            where.append("is_meaningful = 1")

        where_clause = f" WHERE {' AND '.join(where)}" if where else ""
        cur = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM signal_changes{where_clause}",
            params,
        )
        return int(cur.fetchone()["n"])

    # ── Classification write-back (Module 2) ──────────────

    def update_classification(
        self,
        signal_id: str,
        *,
        gap_ids: list[str],
        signal_category: str,
        confidence: float,
        classifier_version: str,
    ) -> bool:
        """Module 2 (Signal Classifier) write-back. See D3.1.

        Sets ``classification_json`` and ``gap_ids`` on the signal row.
        Returns True if signal exists and was updated, False if not found.
        """
        from datetime import datetime, timezone

        cur = self._conn.execute(
            "SELECT 1 FROM signals WHERE signal_id = ? LIMIT 1",
            (signal_id,),
        )
        if cur.fetchone() is None:
            return False

        classified_at = datetime.now(timezone.utc).isoformat()
        classification_json = json.dumps(
            {
                "classified_at": classified_at,
                "gap_ids": gap_ids,
                "signal_category": signal_category,
                "confidence": confidence,
                "classifier_version": classifier_version,
            },
            ensure_ascii=False,
        )
        gap_ids_json = json.dumps(gap_ids, ensure_ascii=False)

        with self._txn():
            self._conn.execute(
                """
                UPDATE signals
                   SET classification_json = ?,
                       gap_ids = ?
                 WHERE signal_id = ?
                """,
                (classification_json, gap_ids_json, signal_id),
            )
        return True
