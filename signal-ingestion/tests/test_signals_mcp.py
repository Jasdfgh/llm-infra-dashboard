"""Unit tests for signals_mcp_server.py tool functions.

Tests the 12 MCP tool functions directly — no MCP protocol layer involved.
Uses a real SQLite DB in tmp_path with FTS5 end-to-end.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "scripts"))

import scripts.signals_mcp_server as server
from src.ingestion.models import GitHubPayload, Signal
from src.storage.database import init_db
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_sig1() -> Signal:
    return Signal(
        signal_id="github:vllm-project/vllm:issue:100",
        source_type="github_issue",
        source_url="https://github.com/vllm-project/vllm/issues/100",
        source_repo="vllm-project/vllm",
        source_number=100,
        title="ROCm aiter MLA performance bug",
        body="This is a test issue about aiter MLA on ROCm MI300X",
        author="testuser",
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat(),
        updated_at=datetime(2026, 4, 20, tzinfo=timezone.utc).isoformat(),
        first_seen_at=datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat(),
        last_synced_at=datetime(2026, 4, 20, tzinfo=timezone.utc).isoformat(),
        content_hash="abc123",
        tags=["rocm", "bug"],
        github=GitHubPayload(
            state="open",
            labels=["rocm", "bug"],
            comment_count=2,
        ),
    )


def _make_sig2() -> Signal:
    return Signal(
        signal_id="github:sgl-project/sglang:issue:200",
        source_type="github_issue",
        source_url="https://github.com/sgl-project/sglang/issues/200",
        source_repo="sgl-project/sglang",
        source_number=200,
        title="AMD GPU memory leak",
        body="Memory leak when using AMD GPU with flash attention",
        author="amddev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc).isoformat(),
        updated_at=datetime(2026, 4, 25, tzinfo=timezone.utc).isoformat(),
        first_seen_at=datetime(2026, 4, 10, tzinfo=timezone.utc).isoformat(),
        last_synced_at=datetime(2026, 4, 25, tzinfo=timezone.utc).isoformat(),
        content_hash="def456",
        tags=["amd", "memory"],
        github=GitHubPayload(
            state="closed",
            labels=["amd"],
            comment_count=0,
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_with_test_db(tmp_path):
    """Create a test DB with two signals and patch the server to use it."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    repo = SignalRepository(db_path=str(db_path))
    repo.upsert_signal(_make_sig1())
    repo.upsert_signal(_make_sig2())

    search_inst = SignalSearch(repo)

    orig_repo = server._repo_instance
    orig_search = server._search_instance
    orig_db_path = server.DB_PATH
    orig_proc = server._running_proc

    server._repo_instance = repo
    server._search_instance = search_inst
    server.DB_PATH = db_path
    server._running_proc = None

    yield repo, search_inst

    server._repo_instance = orig_repo
    server._search_instance = orig_search
    server.DB_PATH = orig_db_path
    server._running_proc = orig_proc
    repo.close()


# =====================================================================
#  Query tool tests
# =====================================================================


class TestSearchSignals:
    """search_signals() — multi-filter FTS5 + indexed column search."""

    def test_fts(self, mcp_with_test_db):
        """FTS query 'aiter MLA' matches sig1's title and body."""
        result = server.search_signals(query="aiter MLA")
        assert "error" not in result
        assert result["total"] >= 1
        ids = [r["signal_id"] for r in result["results"]]
        assert "github:vllm-project/vllm:issue:100" in ids

    def test_repo_filter(self, mcp_with_test_db):
        """Repo filter isolates sig2 (sgl-project/sglang)."""
        result = server.search_signals(repos="sgl-project/sglang")
        assert "error" not in result
        ids = [r["signal_id"] for r in result["results"]]
        assert ids == ["github:sgl-project/sglang:issue:200"]

    def test_state_filter(self, mcp_with_test_db):
        """state=open returns only sig1 (open); excludes sig2 (closed)."""
        result = server.search_signals(state="open")
        assert "error" not in result
        ids = {r["signal_id"] for r in result["results"]}
        assert "github:vllm-project/vllm:issue:100" in ids
        assert "github:sgl-project/sglang:issue:200" not in ids

    def test_labels_filter(self, mcp_with_test_db):
        """labels=rocm matches sig1 only (sig2 has label 'amd')."""
        result = server.search_signals(labels="rocm")
        assert "error" not in result
        ids = {r["signal_id"] for r in result["results"]}
        assert "github:vllm-project/vllm:issue:100" in ids
        assert "github:sgl-project/sglang:issue:200" not in ids

    def test_no_query_no_filter(self, mcp_with_test_db):
        """Empty call returns all signals."""
        result = server.search_signals()
        assert "error" not in result
        assert result["total"] == 2
        assert len(result["results"]) == 2

    def test_combined_filters(self, mcp_with_test_db):
        """repo + state combined narrows to sig1."""
        result = server.search_signals(repos="vllm-project/vllm", state="open")
        assert "error" not in result
        ids = [r["signal_id"] for r in result["results"]]
        assert ids == ["github:vllm-project/vllm:issue:100"]


class TestGetSignalDetail:
    """get_signal_detail() — single-signal full detail."""

    def test_exists(self, mcp_with_test_db):
        """Existing signal returns full detail with title and body."""
        result = server.get_signal_detail(
            signal_id="github:vllm-project/vllm:issue:100"
        )
        assert "error" not in result
        assert result["title"] == "ROCm aiter MLA performance bug"
        assert "aiter MLA" in result["body"]

    def test_not_found(self, mcp_with_test_db):
        """Non-existent signal returns {error: 'not_found'}."""
        result = server.get_signal_detail(signal_id="github:no/repo:issue:999")
        assert result["error"] == "not_found"


class TestGetStats:
    """get_stats() — aggregate statistics."""

    def test_counts(self, mcp_with_test_db):
        """total=2, by_repo contains both repos, by_state has open+closed."""
        result = server.get_stats()
        assert "error" not in result
        assert result["total"] == 2
        assert "vllm-project/vllm" in result["by_repo"]
        assert "sgl-project/sglang" in result["by_repo"]
        assert "open" in result["by_state"]
        assert "closed" in result["by_state"]


class TestExecuteSql:
    """execute_sql() — read-only SQL escape hatch."""

    def test_readonly_select(self, mcp_with_test_db):
        """SELECT COUNT(*) returns correct count."""
        result = server.execute_sql(sql="SELECT COUNT(*) AS n FROM signals")
        assert "error" not in result
        assert result["row_count"] == 1
        assert result["rows"][0][0] == 2

    def test_rejects_write(self, mcp_with_test_db):
        """Write operations (DROP TABLE) are rejected by PRAGMA query_only."""
        result = server.execute_sql(sql="DROP TABLE signals")
        assert "error" in result


# =====================================================================
#  Sync tool tests (no real GitHub — just function signature / format)
# =====================================================================


class TestSyncStatus:
    """sync_status() — process and history check."""

    def test_idle(self, mcp_with_test_db):
        """No running sync process → status 'idle'."""
        result = server.sync_status()
        assert result["current"]["status"] == "idle"


# =====================================================================
#  Management tool tests
# =====================================================================


class TestDbHealth:
    """db_health() — database health check."""

    def test_expected_keys(self, mcp_with_test_db):
        """Response contains all expected keys with correct counts."""
        result = server.db_health()
        assert "error" not in result
        for key in ("db_size_mb", "wal_size_mb", "signal_count",
                     "comment_count", "fts_integrity", "last_sync"):
            assert key in result, f"Missing key: {key}"
        assert result["signal_count"] == 2
        assert result["comment_count"] == 0


# =====================================================================
#  A. get_signal_feed tests
# =====================================================================


class TestGetSignalFeed:
    """get_signal_feed() — incremental keyset-paginated feed."""

    def test_feed_returns_unclassified(self, mcp_with_test_db):
        """get_signal_feed with classified='false' returns unclassified signals."""
        result = server.get_signal_feed(since="2026-01-01T00:00:00Z", classified="false")
        assert "error" not in result
        assert result["pagination"]["returned"] == 2
        ids = {s["signal_id"] for s in result["signals"]}
        assert "github:vllm-project/vllm:issue:100" in ids
        assert "github:sgl-project/sglang:issue:200" in ids

    def test_feed_with_since(self, mcp_with_test_db):
        """get_signal_feed respects since parameter — only sig2 synced after 2026-04-21."""
        result = server.get_signal_feed(since="2026-04-21T00:00:00Z")
        assert "error" not in result
        ids = {s["signal_id"] for s in result["signals"]}
        assert "github:sgl-project/sglang:issue:200" in ids
        assert "github:vllm-project/vllm:issue:100" not in ids

    def test_feed_pagination_keys(self, mcp_with_test_db):
        """Feed response has pagination.total, returned, has_more, next_cursor."""
        result = server.get_signal_feed(since="2026-01-01T00:00:00Z")
        assert "error" not in result
        pag = result["pagination"]
        assert "total" in pag
        assert "returned" in pag
        assert "has_more" in pag
        assert "next_cursor" in pag


# =====================================================================
#  B. get_signal_changes tests
# =====================================================================


class TestGetSignalChanges:
    """get_signal_changes() — change history query."""

    def test_changes_for_existing_signal(self, mcp_with_test_db):
        """get_signal_changes returns change events after inserting one."""
        repo, _ = mcp_with_test_db
        repo.connection.execute(
            "INSERT INTO signal_changes "
            "(signal_id, change_type, changed_at, detected_at, old_value, new_value, is_meaningful) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "github:vllm-project/vllm:issue:100",
                "state_change",
                "2026-04-15T10:00:00Z",
                "2026-04-15T10:00:00Z",
                "open",
                "closed",
                1,
            ),
        )
        repo.connection.commit()

        result = server.get_signal_changes(
            signal_id="github:vllm-project/vllm:issue:100"
        )
        assert "error" not in result
        assert result["count"] >= 1
        assert result["changes"][0]["change_type"] == "state_change"

    def test_changes_for_nonexistent_signal(self, mcp_with_test_db):
        """get_signal_changes for unknown signal returns empty list."""
        result = server.get_signal_changes(signal_id="github:no/repo:issue:999")
        assert "error" not in result
        assert result["count"] == 0
        assert result["changes"] == []


# =====================================================================
#  C. db_maintain tests
# =====================================================================


class TestDbMaintain:
    """db_maintain() — database maintenance actions."""

    def test_analyze(self, mcp_with_test_db):
        """db_maintain(action='analyze') succeeds."""
        result = server.db_maintain(action="analyze")
        assert result["status"] == "ok"
        assert result["action"] == "analyze"

    def test_optimize_fts(self, mcp_with_test_db):
        """db_maintain(action='optimize_fts') succeeds."""
        result = server.db_maintain(action="optimize_fts")
        assert result["status"] == "ok"
        assert result["action"] == "optimize_fts"

    def test_checkpoint(self, mcp_with_test_db):
        """db_maintain(action='checkpoint') succeeds."""
        result = server.db_maintain(action="checkpoint")
        assert result["status"] == "ok"
        assert result["action"] == "checkpoint"
        assert "wal_pages_total" in result

    def test_reconnect(self, mcp_with_test_db):
        """db_maintain(action='reconnect') resets singleton."""
        result = server.db_maintain(action="reconnect")
        assert result["status"] == "reconnected"
        assert result["action"] == "reconnect"
        assert server._repo_instance is None
        assert server._search_instance is None

    def test_invalid_action(self, mcp_with_test_db):
        """db_maintain(action='drop_everything') returns error."""
        result = server.db_maintain(action="drop_everything")
        assert "error" in result
        assert "drop_everything" in result["error"]


# =====================================================================
#  D. trigger_sync parameter tests
# =====================================================================


class TestTriggerSyncParams:
    """trigger_sync() — parameter validation (no actual subprocess)."""

    def test_trigger_sync_invalid_mode(self, mcp_with_test_db):
        """trigger_sync with invalid mode returns error."""
        result = server.trigger_sync(mode="yolo")
        assert "error" in result
        assert "yolo" in result["error"]

    def test_trigger_sync_targeted_without_target(self, mcp_with_test_db):
        """trigger_sync targeted mode without target returns error."""
        result = server.trigger_sync(mode="targeted", target="")
        assert "error" in result
        assert "target" in result["error"].lower()


# =====================================================================
#  E. search_signals advanced parameter tests
# =====================================================================


class TestSearchAdvanced:
    """search_signals() — advanced filter / sort / pagination."""

    def test_since_filter(self, mcp_with_test_db):
        """search_signals(since='2026-04-15') filters by updated_at."""
        result = server.search_signals(since="2026-04-15T00:00:00Z")
        assert "error" not in result
        ids = {r["signal_id"] for r in result["results"]}
        assert "github:vllm-project/vllm:issue:100" in ids
        assert "github:sgl-project/sglang:issue:200" in ids

        result_late = server.search_signals(since="2026-04-22T00:00:00Z")
        assert "error" not in result_late
        ids_late = {r["signal_id"] for r in result_late["results"]}
        assert "github:sgl-project/sglang:issue:200" in ids_late
        assert "github:vllm-project/vllm:issue:100" not in ids_late

    def test_sort_created(self, mcp_with_test_db):
        """search_signals(sort='created') orders by created_at."""
        result = server.search_signals(sort="created")
        assert "error" not in result
        assert len(result["results"]) == 2
        first_id = result["results"][0]["signal_id"]
        assert first_id == "github:sgl-project/sglang:issue:200"

    def test_source_types_filter(self, mcp_with_test_db):
        """search_signals(source_types='github_issue') filters type."""
        result = server.search_signals(source_types="github_issue")
        assert "error" not in result
        assert result["total"] == 2

        result_pr = server.search_signals(source_types="github_pr")
        assert "error" not in result_pr
        assert result_pr["total"] == 0

    def test_offset_pagination(self, mcp_with_test_db):
        """search_signals(offset=1, limit=1) returns second result."""
        full = server.search_signals(sort="updated", limit=10)
        assert "error" not in full
        assert full["total"] == 2

        page = server.search_signals(sort="updated", offset=1, limit=1)
        assert "error" not in page
        assert len(page["results"]) == 1
        assert page["results"][0]["signal_id"] == full["results"][1]["signal_id"]


# =====================================================================
#  F. signal_labels trigger integration
# =====================================================================


class TestLabelsIntegration:
    """signal_labels trigger end-to-end via DB triggers."""

    def test_insert_triggers_label_table(self, mcp_with_test_db):
        """After inserting a signal with labels, signal_labels table has rows."""
        repo, _ = mcp_with_test_db
        rows = repo.connection.execute(
            "SELECT signal_id, label FROM signal_labels ORDER BY signal_id, label"
        ).fetchall()
        label_map: dict[str, set[str]] = {}
        for r in rows:
            label_map.setdefault(r["signal_id"], set()).add(r["label"])

        assert "rocm" in label_map.get("github:vllm-project/vllm:issue:100", set())
        assert "bug" in label_map.get("github:vllm-project/vllm:issue:100", set())
        assert "amd" in label_map.get("github:sgl-project/sglang:issue:200", set())

    def test_search_by_label_uses_index(self, mcp_with_test_db):
        """search_signals(labels='rocm') returns only sig1."""
        result = server.search_signals(labels="rocm")
        assert "error" not in result
        ids = {r["signal_id"] for r in result["results"]}
        assert ids == {"github:vllm-project/vllm:issue:100"}

        result_amd = server.search_signals(labels="amd")
        assert "error" not in result_amd
        ids_amd = {r["signal_id"] for r in result_amd["results"]}
        assert ids_amd == {"github:sgl-project/sglang:issue:200"}
