"""Unit tests for ``src.storage.repository.SignalRepository``.

Uses real SQLite in a ``tmp_path`` fixture — no mocking. Each test gets a
fresh database so tests are fully isolated.
"""

from __future__ import annotations

import json

import pytest

from src.ingestion.models import (
    ChangeEvent,
    Comment,
    GitHubPayload,
    RefType,
    Signal,
    SignalRef,
    SyncMode,
    SyncRun,
    SyncStatus,
)
from src.storage.database import init_db
from src.storage.repository import SignalRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


@pytest.fixture
def repo(db_path):
    r = SignalRepository(db_path)
    yield r
    r.close()


@pytest.fixture
def sample_signal():
    """A minimal valid Signal for testing."""
    return Signal(
        signal_id="github:test/repo:issue:1",
        source_type="github_issue",
        source_url="https://github.com/test/repo/issues/1",
        source_repo="test/repo",
        source_number=1,
        title="Test issue",
        body="Test body content about ROCm and aiter MLA",
        body_token_estimate=20,
        author="alice",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        first_seen_at="2026-04-20T00:00:00Z",
        last_synced_at="2026-04-20T00:00:00Z",
        content_hash="a" * 64,
        tags=["bug", "rocm"],
        github=GitHubPayload(
            state="open", labels=["bug", "rocm"], comment_count=5
        ),
    )


def _make_signal(suffix: str, **overrides) -> Signal:
    """Helper to create distinct Signal instances."""
    defaults = dict(
        signal_id=f"github:test/repo:issue:{suffix}",
        source_type="github_issue",
        source_url=f"https://github.com/test/repo/issues/{suffix}",
        source_repo="test/repo",
        source_number=int(suffix) if suffix.isdigit() else 999,
        title=f"Issue {suffix}",
        body=f"Body for {suffix}",
        body_token_estimate=10,
        author="alice",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        first_seen_at="2026-04-20T00:00:00Z",
        last_synced_at="2026-04-20T00:00:00Z",
        content_hash="b" * 64,
        tags=["test"],
        github=GitHubPayload(state="open", labels=["test"], comment_count=0),
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---------------------------------------------------------------------------
# upsert_signal
# ---------------------------------------------------------------------------


class TestUpsertSignal:
    def test_insert_new_signal(self, repo, sample_signal):
        is_new, version = repo.upsert_signal(sample_signal)
        assert is_new is True
        assert version == 1

    def test_update_existing_signal_bumps_version(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        is_new, version = repo.upsert_signal(sample_signal)
        assert is_new is False
        assert version == 2

    def test_update_with_version_override_keeps_version(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        is_new, version = repo.upsert_signal(
            sample_signal, version_override=1
        )
        assert is_new is False
        assert version == 1

    def test_update_preserves_existing_comments(self, repo, sample_signal):
        """UPDATE must not destroy child rows (proves we don't use INSERT OR REPLACE)."""
        repo.upsert_signal(sample_signal)

        comments = [
            Comment(
                signal_id=sample_signal.signal_id,
                comment_id="c1",
                author="bob",
                body="A comment",
                created_at="2026-04-02T00:00:00Z",
            ),
        ]
        repo.upsert_comments(comments)
        assert len(repo.get_comments(sample_signal.signal_id)) == 1

        repo.upsert_signal(sample_signal)

        remaining = repo.get_comments(sample_signal.signal_id)
        assert len(remaining) == 1, "UPDATE wiped comments — likely INSERT OR REPLACE"


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    def test_existing_signal(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        row = repo.get_by_id(sample_signal.signal_id)
        assert row is not None
        assert row["signal_id"] == sample_signal.signal_id
        assert row["title"] == "Test issue"
        assert isinstance(row["tags"], str)

    def test_nonexistent_signal(self, repo):
        assert repo.get_by_id("does:not:exist") is None


# ---------------------------------------------------------------------------
# upsert_comments
# ---------------------------------------------------------------------------


class TestUpsertComments:
    def test_insert_two_comments(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        comments = [
            Comment(
                signal_id=sample_signal.signal_id,
                comment_id="c1",
                author="bob",
                body="First",
                created_at="2026-04-02T00:00:00Z",
            ),
            Comment(
                signal_id=sample_signal.signal_id,
                comment_id="c2",
                author="carol",
                body="Second",
                created_at="2026-04-03T00:00:00Z",
            ),
        ]
        inserted = repo.upsert_comments(comments)
        assert inserted == 2

    def test_duplicate_comments_ignored(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        c = Comment(
            signal_id=sample_signal.signal_id,
            comment_id="c1",
            author="bob",
            body="Only once",
            created_at="2026-04-02T00:00:00Z",
        )
        repo.upsert_comments([c])
        inserted = repo.upsert_comments([c])
        assert inserted == 0

    def test_comments_ordered_by_created_at(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        comments = [
            Comment(
                signal_id=sample_signal.signal_id,
                comment_id="c_late",
                author="bob",
                body="Late",
                created_at="2026-04-10T00:00:00Z",
            ),
            Comment(
                signal_id=sample_signal.signal_id,
                comment_id="c_early",
                author="carol",
                body="Early",
                created_at="2026-04-01T00:00:00Z",
            ),
        ]
        repo.upsert_comments(comments)
        rows = repo.get_comments(sample_signal.signal_id)
        assert rows[0]["comment_id"] == "c_early"
        assert rows[1]["comment_id"] == "c_late"


# ---------------------------------------------------------------------------
# append_changes
# ---------------------------------------------------------------------------


class TestAppendChanges:
    def test_append_two_changes(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        changes = [
            ChangeEvent(
                signal_id=sample_signal.signal_id,
                change_type="state_change",
                changed_at="2026-04-10T00:00:00Z",
                detected_at="2026-04-10T00:01:00Z",
                old_value='"open"',
                new_value='"closed"',
            ),
            ChangeEvent(
                signal_id=sample_signal.signal_id,
                change_type="label_change",
                changed_at="2026-04-11T00:00:00Z",
                detected_at="2026-04-11T00:01:00Z",
                old_value='["bug"]',
                new_value='["bug","wontfix"]',
            ),
        ]
        inserted = repo.append_changes(changes)
        assert inserted == 2

        rows = repo.get_changes(sample_signal.signal_id)
        assert len(rows) >= 2


# ---------------------------------------------------------------------------
# update_classification
# ---------------------------------------------------------------------------


class TestUpdateClassification:
    def test_classify_existing_signal(self, repo, sample_signal):
        repo.upsert_signal(sample_signal)
        ok = repo.update_classification(
            sample_signal.signal_id,
            gap_ids=["GAP-001", "GAP-002"],
            signal_category="feature_request",
            confidence=0.95,
            classifier_version="v0.1",
        )
        assert ok is True

        row = repo.get_by_id(sample_signal.signal_id)
        cls_data = json.loads(row["classification_json"])
        assert cls_data["gap_ids"] == ["GAP-001", "GAP-002"]
        assert cls_data["signal_category"] == "feature_request"
        assert cls_data["confidence"] == 0.95
        gap_ids = json.loads(row["gap_ids"])
        assert gap_ids == ["GAP-001", "GAP-002"]

    def test_classify_nonexistent_signal(self, repo):
        ok = repo.update_classification(
            "does:not:exist",
            gap_ids=["GAP-001"],
            signal_category="bug",
            confidence=0.5,
            classifier_version="v0.1",
        )
        assert ok is False


# ---------------------------------------------------------------------------
# sync_runs
# ---------------------------------------------------------------------------


class TestSyncRuns:
    def test_create_and_update_sync_run(self, repo):
        run = SyncRun(
            id="sync_test_20260420_000000",
            source_type="github",
            source_repo="test/repo",
            sync_mode=SyncMode.FULL,
            started_at="2026-04-20T00:00:00Z",
            status=SyncStatus.RUNNING,
        )
        repo.create_sync_run(run)

        repo.update_sync_run(
            run.id,
            status=SyncStatus.COMPLETED,
            completed_at="2026-04-20T01:00:00Z",
            signals_total=10,
            signals_created=5,
            signals_updated=3,
            signals_unchanged=2,
        )

        row = repo.get_last_successful_sync("test/repo")
        assert row is not None
        assert row["status"] == "completed"
        assert row["signals_total"] == 10
        assert row["completed_at"] == "2026-04-20T01:00:00Z"

    def test_get_last_successful_sync_returns_most_recent(self, repo):
        for i, ts in enumerate(["2026-04-18", "2026-04-19", "2026-04-20"]):
            run = SyncRun(
                id=f"sync_{i}",
                source_type="github",
                source_repo="test/repo",
                sync_mode="full",
                started_at=f"{ts}T00:00:00Z",
                status=SyncStatus.COMPLETED,
                completed_at=f"{ts}T01:00:00Z",
            )
            repo.create_sync_run(run)

        last = repo.get_last_successful_sync("test/repo")
        assert last is not None
        assert last["id"] == "sync_2"


# ---------------------------------------------------------------------------
# transaction()
# ---------------------------------------------------------------------------


class TestTransaction:
    def test_atomic_commit(self, repo, sample_signal):
        """Multiple operations inside transaction() commit together."""
        with repo.transaction():
            repo.upsert_signal(sample_signal)
            repo.upsert_comments(
                [
                    Comment(
                        signal_id=sample_signal.signal_id,
                        comment_id="txn_c1",
                        author="dave",
                        body="Inside txn",
                        created_at="2026-04-05T00:00:00Z",
                    )
                ]
            )

        assert repo.get_by_id(sample_signal.signal_id) is not None
        assert len(repo.get_comments(sample_signal.signal_id)) == 1

    def test_rollback_on_exception(self, db_path, sample_signal):
        """Injected-connection mode: transaction() rolls back all ops on exception.

        NOTE: With owned connections, each write method's ``_txn()`` calls
        ``with self._conn:`` which auto-commits *before* the outer
        ``transaction()`` gets a chance to rollback — a known limitation
        of the nested ``with conn:`` pattern in Python's sqlite3 module.
        Injected-connection mode (``_owns_conn=False``) avoids this because
        ``_txn()`` becomes a no-op and ``transaction()`` is the sole commit
        boundary.
        """
        from src.storage.database import dict_row_factory, get_connection

        with SignalRepository(db_path) as setup_repo:
            setup_repo.upsert_signal(sample_signal)

        conn = get_connection(db_path)
        conn.row_factory = dict_row_factory
        try:
            repo = SignalRepository(connection=conn)

            with pytest.raises(RuntimeError):
                with repo.transaction():
                    repo.upsert_comments(
                        [
                            Comment(
                                signal_id=sample_signal.signal_id,
                                comment_id="will_rollback",
                                author="eve",
                                body="Should not persist",
                                created_at="2026-04-06T00:00:00Z",
                            )
                        ]
                    )
                    raise RuntimeError("boom")

            assert len(repo.get_comments(sample_signal.signal_id)) == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# reconcile_refs
# ---------------------------------------------------------------------------


class TestReconcileRefs:
    def test_reconcile_fills_to_signal_id(self, repo):
        sig_a = _make_signal("100", source_number=100)
        sig_b = _make_signal(
            "200",
            source_number=200,
            source_url="https://github.com/test/repo/issues/200",
        )
        repo.upsert_signal(sig_a)
        repo.upsert_signal(sig_b)

        ref = SignalRef(
            from_signal_id=sig_a.signal_id,
            to_signal_id=None,
            to_url="https://github.com/test/repo/issues/200",
            ref_type=RefType.MENTIONS,
            created_at="2026-04-20T00:00:00Z",
        )
        repo.upsert_refs([ref])

        updated = repo.reconcile_refs()
        assert updated == 1

        rows = repo.connection.execute(
            "SELECT to_signal_id FROM signal_refs WHERE from_signal_id = ?",
            (sig_a.signal_id,),
        ).fetchall()
        assert rows[0]["to_signal_id"] == sig_b.signal_id
