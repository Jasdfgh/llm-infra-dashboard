"""Unit tests for signals_mcp_server.py tool functions.

Tests the 12 MCP tool functions directly — no MCP protocol layer involved.
Uses a real SQLite DB in tmp_path with FTS5 end-to-end.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# =====================================================================
#  G. execute_sql security mechanism tests
# =====================================================================


class TestExecuteSqlSecurity:
    """execute_sql() — four-layer defense: authorizer, blacklist, read-only, progress."""

    def test_execute_sql_rejects_attach(self, mcp_with_test_db):
        """ATTACH DATABASE is denied by the authorizer (non-READ/SELECT action)."""
        result = server.execute_sql(sql="ATTACH DATABASE ':memory:' AS x")
        assert "error" in result, "ATTACH should be blocked by authorizer"

    def test_execute_sql_rejects_forbidden_pragma(self, mcp_with_test_db):
        """Non-whitelisted PRAGMAs are denied (e.g. PRAGMA key)."""
        result = server.execute_sql(sql="PRAGMA key = 'secret'")
        assert "error" in result, "PRAGMA key is not in _SAFE_PRAGMAS whitelist"

    def test_execute_sql_allows_whitelisted_pragma(self, mcp_with_test_db):
        """Whitelisted PRAGMAs like table_info work and return column info."""
        result = server.execute_sql(sql="PRAGMA table_info(signals)")
        assert "error" not in result, f"table_info should be allowed: {result.get('error')}"
        assert result["row_count"] > 0, "signals table should have columns"
        assert len(result["columns"]) > 0

    def test_execute_sql_rejects_blacklisted_function(self, mcp_with_test_db):
        """Blacklisted functions (randomblob, load_extension) are denied."""
        result = server.execute_sql(sql="SELECT randomblob(100)")
        assert "error" in result, "randomblob is in _BLOCKED_FUNCTIONS"

        result2 = server.execute_sql(sql="SELECT load_extension('x')")
        assert "error" in result2, "load_extension is in _BLOCKED_FUNCTIONS"

    def test_execute_sql_rejects_write_operations(self, mcp_with_test_db):
        """Write operations are blocked (read-only URI + authorizer)."""
        result_insert = server.execute_sql(
            sql="INSERT INTO signals (signal_id, title) VALUES ('x', 'y')"
        )
        assert "error" in result_insert, "INSERT should be denied on read-only connection"

        result_drop = server.execute_sql(sql="DROP TABLE signals")
        assert "error" in result_drop, "DROP TABLE should be denied"


# =====================================================================
#  H. trigger_sync repo whitelist tests
# =====================================================================


class TestTriggerSyncRepoWhitelist:
    """trigger_sync() — repo validation and whitelist enforcement."""

    def test_trigger_sync_rejects_unknown_repo(self, mcp_with_test_db):
        """Repo not in sources.yaml is rejected with allowed list shown."""
        result = server.trigger_sync(repo="unknown-org/unknown-repo")
        assert "error" in result, "Unknown repo should be rejected"
        assert "not in the allowed list" in result["error"]
        assert "Allowed:" in result["error"]

    def test_trigger_sync_rejects_invalid_repo_name(self, mcp_with_test_db):
        """Path-traversal style repo names are rejected by pattern check."""
        result = server.trigger_sync(repo="../../../etc/passwd")
        assert "error" in result, "Path-traversal repo name should be rejected"
        assert "Invalid repo name" in result["error"]

    def test_trigger_sync_valid_repo_accepted(self, mcp_with_test_db, tmp_path):
        """Repo from sources.yaml passes validation and starts subprocess."""
        (tmp_path / "data").mkdir(exist_ok=True)
        with patch("scripts.signals_mcp_server._ROOT", tmp_path), \
             patch("scripts.signals_mcp_server.subprocess.Popen") as mock_popen, \
             patch("scripts.signals_mcp_server._acquire_sync_lock", return_value=99), \
             patch("scripts.signals_mcp_server._release_sync_lock"):
            mock_proc = MagicMock(pid=12345)
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc
            result = server.trigger_sync(repo="vllm-project/vllm")
            assert "error" not in result, f"Valid repo should not error: {result}"
            assert result["status"] == "started"
            assert result["pid"] == 12345
            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            assert "sync_github.py" in str(cmd)
            assert "vllm-project/vllm" in cmd


# =====================================================================
#  I. Audit log tests
# =====================================================================


class TestAuditLog:
    """Audit logging — write entries and cleanup old files."""

    def test_audit_monkey_patch_installed(self, mcp_with_test_db):
        """Verify _tool_manager.call_tool points to the audited wrapper."""
        import scripts.signals_mcp_server as srv
        mgr = getattr(srv.mcp, "_tool_manager", None)
        assert mgr is not None, "mcp._tool_manager should exist"
        assert srv._orig_call_tool is not None, "Original call_tool should be saved"
        assert mgr.call_tool is not srv._orig_call_tool, \
            "call_tool should be patched to the audited wrapper"
        assert mgr.call_tool is srv._audited_call_tool, \
            "call_tool should point to _audited_call_tool"

    def test_audit_log_written_on_tool_call(self, tmp_path, monkeypatch):
        """Calling _write_audit_entry creates a JSONL file with correct content."""
        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        server._write_audit_entry(
            tool="execute_sql",
            args={"sql": "SELECT 1"},
            status="success",
            elapsed_ms=12.3,
            result_summary={"type": "dict", "keys": 3},
        )

        log_files = list(tmp_path.glob("mcp_audit_*.jsonl"))
        assert len(log_files) == 1, "Audit log file should be created"

        import json as _json
        content = log_files[0].read_text().strip()
        entry = _json.loads(content)
        assert entry["tool"] == "execute_sql"
        assert entry["args"]["sql"] == "SELECT 1"
        assert entry["status"] == "success"
        assert entry["elapsed_ms"] == 12.3

    def test_audited_call_tool_wrapper_logs_success(self, mcp_with_test_db, tmp_path, monkeypatch):
        """_audited_call_tool wrapper writes audit log with status='success'."""
        import asyncio
        import json as _json

        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        async def _run():
            return await server._audited_call_tool("get_stats", {})

        asyncio.new_event_loop().run_until_complete(_run())

        log_files = list(tmp_path.glob("mcp_audit_*.jsonl"))
        assert len(log_files) == 1, "Audit log should be created by wrapper"

        entry = _json.loads(log_files[0].read_text().strip())
        assert entry["tool"] == "get_stats"
        assert entry["status"] == "success"

    def test_audited_call_tool_wrapper_logs_tool_error(self, mcp_with_test_db, tmp_path, monkeypatch):
        """_audited_call_tool wrapper detects error in result and logs status='tool_error'."""
        import asyncio
        import json as _json

        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        async def _run():
            return await server._audited_call_tool("execute_sql", {"sql": "DROP TABLE signals"})

        asyncio.new_event_loop().run_until_complete(_run())

        log_files = list(tmp_path.glob("mcp_audit_*.jsonl"))
        assert len(log_files) == 1

        entry = _json.loads(log_files[0].read_text().strip())
        assert entry["tool"] == "execute_sql"
        assert entry["status"] == "tool_error"

    def test_write_audit_entry_direct_success(self, mcp_with_test_db, tmp_path, monkeypatch):
        """Direct _write_audit_entry records success correctly (unit-level test)."""
        import json as _json

        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        result = server.get_stats()
        assert "error" not in result

        server._write_audit_entry(
            tool="get_stats",
            args={},
            status="success",
            elapsed_ms=5.0,
            result_summary=server._extract_result_summary(result),
        )

        log_files = list(tmp_path.glob("mcp_audit_*.jsonl"))
        assert len(log_files) == 1

        entry = _json.loads(log_files[0].read_text().strip())
        assert entry["tool"] == "get_stats"
        assert entry["status"] == "success"
        assert "total" in entry["result_summary"]

    def test_write_audit_entry_direct_tool_error(self, mcp_with_test_db, tmp_path, monkeypatch):
        """Direct _write_audit_entry records tool_error correctly (unit-level test)."""
        import json as _json

        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        result = server.execute_sql(sql="DROP TABLE signals")
        assert "error" in result
        assert server._result_indicates_error(result) is True

        server._write_audit_entry(
            tool="execute_sql",
            args={"sql": "DROP TABLE signals"},
            status="tool_error",
            elapsed_ms=2.0,
            result_summary=server._extract_result_summary(result),
        )

        log_files = list(tmp_path.glob("mcp_audit_*.jsonl"))
        assert len(log_files) == 1

        entry = _json.loads(log_files[0].read_text().strip())
        assert entry["tool"] == "execute_sql"
        assert entry["status"] == "tool_error"
        assert "error" in entry["result_summary"]

    def test_audit_log_cleanup_old_files(self, tmp_path, monkeypatch):
        """_cleanup_old_audit_logs removes files with expired week numbers."""
        monkeypatch.setattr(server, "_AUDIT_DIR", tmp_path)

        now = datetime.now(timezone.utc)
        cur_year, cur_week, _ = now.isocalendar()

        current_file = tmp_path / f"mcp_audit_{cur_year}-W{cur_week:02d}.jsonl"
        current_file.write_text('{"tool":"test"}\n')

        old_week = cur_week - 5 if cur_week > 5 else 1
        old_file = tmp_path / f"mcp_audit_{cur_year - 1}-W{old_week:02d}.jsonl"
        old_file.write_text('{"tool":"ancient"}\n')

        server._cleanup_old_audit_logs()

        assert current_file.exists(), "Current week's log should be preserved"
        assert not old_file.exists(), "Expired audit log should be deleted"


# =====================================================================
#  J. Stale sync_run auto-cleanup tests
# =====================================================================


class TestStaleSyncRunCleanup:
    """sync_status() auto-cleans stale running sync_runs."""

    def test_sync_status_auto_cleans_stale_runs(self, mcp_with_test_db, tmp_path):
        """A sync_run running for >4h is auto-marked as failed."""
        repo, _ = mcp_with_test_db
        conn = repo.connection

        stale_start = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        conn.execute(
            "INSERT INTO sync_runs (id, source_type, source_repo, sync_mode, status, started_at) "
            "VALUES (?, 'github', ?, 'incremental', 'running', ?)",
            ("stale_test_run_001", "test-org/test-repo", stale_start),
        )
        conn.commit()

        lock_file = tmp_path / "test-sync.lock"
        lock_file.touch()
        with patch("scripts.signals_mcp_server._SYNC_LOCK_FILE", str(lock_file)):
            result = server.sync_status()

        assert "error" not in result
        assert "auto_cleaned_stale" in result
        assert "stale_test_run_001" in result["auto_cleaned_stale"]

        row = conn.execute(
            "SELECT status, error_message FROM sync_runs WHERE id = ?",
            ("stale_test_run_001",),
        ).fetchone()
        assert row["status"] == "failed"
        assert "Stale: auto-detected by sync_status" in row["error_message"]

    def test_sync_status_preserves_stale_when_lock_held(self, mcp_with_test_db, tmp_path):
        """Stale run is NOT cleaned when the sync lock is held, then cleaned after release."""
        import fcntl

        repo, _ = mcp_with_test_db
        conn = repo.connection

        lock_file = tmp_path / "test-sync.lock"
        lock_file.touch()

        stale_start = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        conn.execute(
            "INSERT INTO sync_runs (id, source_type, source_repo, sync_mode, status, started_at) "
            "VALUES (?, 'github', ?, 'incremental', 'running', ?)",
            ("stale_lock_test_001", "test-org/test-repo", stale_start),
        )
        conn.commit()

        with patch("scripts.signals_mcp_server._SYNC_LOCK_FILE", str(lock_file)):
            fd = open(str(lock_file), "w")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)

                result = server.sync_status()
                assert "error" not in result
                row = conn.execute(
                    "SELECT status FROM sync_runs WHERE id = ?",
                    ("stale_lock_test_001",),
                ).fetchone()
                assert row["status"] == "running", \
                    "Stale row should NOT be cleaned while lock is held"
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()

            result2 = server.sync_status()
            assert "error" not in result2
            assert "auto_cleaned_stale" in result2
            assert "stale_lock_test_001" in result2["auto_cleaned_stale"]
            row2 = conn.execute(
                "SELECT status, error_message FROM sync_runs WHERE id = ?",
                ("stale_lock_test_001",),
            ).fetchone()
            assert row2["status"] == "failed"
            assert "Stale" in row2["error_message"]
