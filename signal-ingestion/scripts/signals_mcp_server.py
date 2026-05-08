#!/usr/bin/env python3
"""Signals MCP server — 12 tools for downstream agents.

Exposes search, sync-control, and DB-management tools over MCP HTTP.
Replaces the 3-tool sync_mcp_server.py with a unified service.

Usage:
    .venv/bin/python scripts/signals_mcp_server.py                # localhost:8082
    .venv/bin/python scripts/signals_mcp_server.py --port 9090    # custom port
    .venv/bin/python scripts/signals_mcp_server.py --host 0.0.0.0 # expose to LAN (internal network only)

Local config (same machine):
    "signals": { "url": "http://localhost:8082/mcp" }

LAN config (internal network, no auth):
    "signals": { "url": "http://<server-ip>:8082/mcp" }
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

mcp = FastMCP(
    "Signals Service",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

DB_PATH = _PROJ / "data" / "signals.db"
SYNC_SCRIPT = _PROJ / "scripts" / "sync_github.py"
INCR_SCRIPT = _PROJ / "scripts" / "incremental_sync.sh"
VENV_PYTHON = _PROJ / ".venv" / "bin" / "python"
_SOURCES_YAML = _PROJ / "config" / "sources.yaml"
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SYNC_LOCK_FILE = "/tmp/signals-sync.lock"
_QUERY_STEP_LIMIT = 5

logger = logging.getLogger("signals_mcp")

# ── Singleton DB instances (lazy) ────────────────────────────

_repo_instance: SignalRepository | None = None
_search_instance: SignalSearch | None = None


def _get_repo() -> SignalRepository:
    global _repo_instance
    if _repo_instance is None:
        _repo_instance = SignalRepository(db_path=str(DB_PATH))
        _repo_instance.connection.isolation_level = None
        _repo_instance.connection.execute("PRAGMA wal_autocheckpoint = 200")
    return _repo_instance


def _get_search() -> SignalSearch:
    global _search_instance
    if _search_instance is None:
        _search_instance = SignalSearch(_get_repo())
    return _search_instance


# ── Helper: comma-string → list ─────────────────────────────

def _split(csv: str) -> list[str] | None:
    """Split comma-separated string into list. Returns None for empty input."""
    if not csv or not csv.strip():
        return None
    return [s.strip() for s in csv.split(",") if s.strip()]


def _parse_bool_tri(value: str) -> bool | None:
    """Parse three-state string: 'true' → True, 'false' → False, 'all'/'' → None."""
    v = value.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return None


# ── SQL authorizer for read-only execute_sql ─────────────────

_SAFE_PRAGMAS = frozenset({
    "database_list", "table_info", "table_xinfo", "index_list",
    "index_info", "compile_options", "function_list",
})

_BLOCKED_FUNCTIONS = frozenset({
    "randomblob", "zeroblob", "writefile", "readfile",
    "edit", "load_extension",
})


def _sql_authorizer(action, arg1, arg2, db_name, trigger_name):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        if arg2 and arg2.lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        if arg1 and arg1.lower() in _SAFE_PRAGMAS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


_MAX_CELL_BYTES = 10_000
_MAX_RESPONSE_BYTES = 5_000_000


def _sanitize_cell(value):
    """Ensure a cell value is JSON-serializable and bounded in size.

    Returns (sanitized_value, was_truncated).
    """
    if isinstance(value, bytes):
        if len(value) > 1024:
            return f"<blob {len(value)} bytes>", True
        return value.hex(), False
    if isinstance(value, str) and len(value) > _MAX_CELL_BYTES:
        return value[:_MAX_CELL_BYTES] + f"... (truncated, {len(value)} total chars)", True
    return value, False


# ── Repo whitelist for sync tools ────────────────────────────

def _load_allowed_repos() -> frozenset[str]:
    """Load allowed repo set from config/sources.yaml.
    Returns empty frozenset if config cannot be loaded (fail-closed).
    """
    if yaml is None:
        logger.warning("PyYAML not available; cannot load repo config")
        return frozenset()
    try:
        with open(_SOURCES_YAML) as f:
            cfg = yaml.safe_load(f)
        return frozenset(
            r["repo"] for r in cfg.get("github", {}).get("repos", [])
            if isinstance(r, dict) and "repo" in r
        )
    except Exception:
        logger.exception("Failed to load %s", _SOURCES_YAML)
        return frozenset()


# ── Sync lock (shared with incremental_sync.sh / full_sync_and_report.sh) ──


def _acquire_sync_lock() -> int | None:
    """Try to acquire the shared sync lock. Returns fd on success, None if locked."""
    try:
        fd = os.open(_SYNC_LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        return None


def _release_sync_lock(fd: int) -> None:
    """Release the sync lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except (OSError, IOError):
        pass


# ── Sync subprocess management ───────────────────────────────

_running_proc: subprocess.Popen | None = None


def _reap_finished_sync() -> None:
    """If the sync process has finished, clear the proc reference."""
    global _running_proc
    if _running_proc is not None and _running_proc.poll() is not None:
        _running_proc = None


# =====================================================================
#  Query Tools (7)
# =====================================================================


@mcp.tool()
def search_signals(
    query: str = "",
    repos: str = "",
    source_types: str = "",
    labels: str = "",
    state: str = "",
    since: str = "",
    until: str = "",
    sort: str = "updated",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Multi-filter search over 63k+ signals (FTS5 + indexed columns).

    Args:
        query: Full-text search expression (FTS5 syntax: AND/OR/NOT, "phrases", prefix*). Empty = skip FTS, use filters only.
        repos: Comma-separated repo filter, e.g. "vllm-project/vllm,sgl-project/sglang".
        source_types: Comma-separated type filter, e.g. "github_issue,github_pr".
        labels: Comma-separated label filter (AND logic), e.g. "rocm,bug". Each label must be present.
        state: "open", "closed", or "all". Empty = no state filter.
        since: ISO 8601 UTC lower bound on updated_at, e.g. "2026-04-20T00:00:00Z".
        until: ISO 8601 UTC upper bound on updated_at.
        sort: "relevance" (requires query), "updated", or "created". Default "updated".
        limit: Results per page, 1-50. Default 20.
        offset: Pagination offset. Default 0.

    Returns:
        {results: [...], total: int, meta: {query_time_ms, filters_applied}}
    """
    try:
        search = _get_search()
        return search.search(
            query=query or None,
            repos=_split(repos),
            source_types=_split(source_types),
            labels=_split(labels),
            state=state if state in ("open", "closed", "all") else None,
            since=since or None,
            until=until or None,
            sort=sort if sort in ("relevance", "updated", "created") else "updated",
            limit=max(1, min(limit, 50)),
            offset=max(0, offset),
        )
    except Exception as exc:
        logger.exception("search_signals failed")
        return {"error": str(exc)}


@mcp.tool()
def get_signal_detail(
    signal_id: str,
    include_body: bool = True,
    include_comments: bool = True,
    max_comments: int = 50,
) -> dict:
    """Fetch full detail for a single signal by its ID.

    Args:
        signal_id: Unique signal identifier, e.g. "github:vllm-project/vllm:issue:39303".
        include_body: Include full body text (otherwise 200-char preview only). Default True.
        include_comments: Include comments list. Default True.
        max_comments: Max comments to return, 1-200. Default 50.

    Returns:
        Full signal dict with all parsed JSON fields, or {error: "not_found"}.
    """
    try:
        search = _get_search()
        result = search.get_detail(
            signal_id,
            include_body=include_body,
            include_comments=include_comments,
            max_comments=max(1, min(max_comments, 200)),
        )
        if result is None:
            return {"error": "not_found", "signal_id": signal_id}
        return result
    except Exception as exc:
        logger.exception("get_signal_detail failed")
        return {"error": str(exc)}


@mcp.tool()
def get_signal_feed(
    since: str,
    classified: str = "false",
    source_types: str = "",
    limit: int = 100,
    cursor: str = "",
) -> dict:
    """Incremental feed of signals updated since a given timestamp (keyset pagination).

    Designed for Module 2 consumers that poll for new/updated signals.

    Args:
        since: Required. ISO 8601 UTC, e.g. "2026-04-20T00:00:00Z". Matches last_synced_at >= since.
        classified: "false" = unclassified only (default), "true" = classified only, "all" = no filter.
        source_types: Comma-separated type filter, e.g. "github_issue,github_pr".
        limit: Page size, 1-500. Default 100.
        cursor: Opaque cursor from previous response's pagination.next_cursor. Empty = first page.

    Returns:
        {signals: [...], pagination: {total, returned, next_cursor, has_more}, meta: {...}}
    """
    try:
        search = _get_search()
        return search.get_feed(
            since=since,
            classified=_parse_bool_tri(classified),
            source_types=_split(source_types),
            limit=max(1, min(limit, 500)),
            cursor=cursor or None,
        )
    except Exception as exc:
        logger.exception("get_signal_feed failed")
        return {"error": str(exc)}


@mcp.tool()
def get_signal_changes(
    signal_id: str = "",
    since: str = "",
    meaningful_only: bool = True,
    limit: int = 100,
) -> dict:
    """Query change history (state transitions, label changes, etc.).

    Args:
        signal_id: Filter to one signal. Empty = changes across all signals.
        since: ISO 8601 UTC lower bound on detected_at. Empty = no time filter.
        meaningful_only: Exclude bot-noise events. Default True.
        limit: Max rows, 1-500. Default 100.

    Returns:
        {changes: [...], count: int}
    """
    try:
        repo = _get_repo()
        changes = repo.get_changes(
            signal_id=signal_id or None,
            since=since or None,
            meaningful_only=meaningful_only,
            limit=max(1, min(limit, 500)),
        )
        return {"changes": changes, "count": len(changes)}
    except Exception as exc:
        logger.exception("get_signal_changes failed")
        return {"error": str(exc)}


@mcp.tool()
def get_gap_signals(
    gap_id: str,
    limit: int = 20,
) -> dict:
    """Find signals classified under a specific gap ID.

    Args:
        gap_id: Gap identifier, e.g. "gap_001".
        limit: Max results, 1-50. Default 20.

    Returns:
        {results: [...], total: int, meta: {...}}
    """
    try:
        search = _get_search()
        return search.search(
            gap_ids=[gap_id],
            limit=max(1, min(limit, 50)),
        )
    except Exception as exc:
        logger.exception("get_gap_signals failed")
        return {"error": str(exc)}


@mcp.tool()
def get_stats() -> dict:
    """Aggregate statistics: signal counts by repo/state/type, last sync time, DB size.

    Returns:
        {by_repo: {...}, by_state: {...}, by_type: {...}, total: int,
         last_sync: {...}, db_size_mb: float}
    """
    try:
        conn = _get_repo().connection

        rows = conn.execute(
            "SELECT source_repo, source_type, github_state, cnt "
            "FROM signal_stats WHERE cnt > 0"
        ).fetchall()

        total = 0
        by_repo: dict[str, int] = {}
        by_state: dict[str, int] = {}
        by_type: dict[str, int] = {}

        for r in rows:
            c = r["cnt"]
            total += c

            repo = r["source_repo"]
            by_repo[repo] = by_repo.get(repo, 0) + c

            state = r["github_state"] or "unknown"
            by_state[state] = by_state.get(state, 0) + c

            stype = r["source_type"]
            by_type[stype] = by_type.get(stype, 0) + c

        by_repo = dict(sorted(by_repo.items(), key=lambda x: x[1], reverse=True))
        by_state = dict(sorted(by_state.items(), key=lambda x: x[1], reverse=True))
        by_type = dict(sorted(by_type.items(), key=lambda x: x[1], reverse=True))

        last_sync_row = conn.execute(
            "SELECT id, source_repo, status, started_at, completed_at "
            "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        last_sync = dict(last_sync_row) if last_sync_row else None

        db_size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 1) if DB_PATH.exists() else 0

        return {
            "total": total,
            "by_repo": by_repo,
            "by_state": by_state,
            "by_type": by_type,
            "last_sync": last_sync,
            "db_size_mb": db_size_mb,
        }
    except Exception as exc:
        logger.exception("get_stats failed")
        return {"error": str(exc)}


@mcp.tool()
def execute_sql(
    sql: str,
    max_rows: int = 100,
) -> dict:
    """Execute a read-only SQL query against signals.db (escape hatch).

    The connection is forced read-only via PRAGMA query_only=ON. Only SELECT statements work.

    Args:
        sql: SQL query to execute, e.g. "SELECT signal_id, title FROM signals WHERE github_state='open' LIMIT 10".
        max_rows: Max rows to return, 1-1000. Default 100.

    Returns:
        {columns: [...], rows: [[...], ...], row_count: int} or {error: str}.
    """
    conn = None
    try:
        max_rows = max(1, min(max_rows, 1000))
        db_uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(_sql_authorizer)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)

        steps = [0]
        def _progress():
            steps[0] += 1
            return 1 if steps[0] > _QUERY_STEP_LIMIT else 0
        conn.set_progress_handler(_progress, 1_000_000)

        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)

        columns = [desc[0] for desc in cur.description] if cur.description else []
        any_truncated = False
        sanitized_data = []
        for row in rows:
            sanitized_row = []
            for cell in row:
                val, trunc = _sanitize_cell(cell)
                sanitized_row.append(val)
                if trunc:
                    any_truncated = True
            sanitized_data.append(sanitized_row)
        data = sanitized_data

        total_available = len(data)
        if len(data) == max_rows:
            extra = cur.fetchone()
            if extra is not None:
                total_available = -1

        result: dict = {
            "columns": columns,
            "rows": data,
            "row_count": len(data),
        }
        if any_truncated:
            result["cell_truncated"] = True
            if "message" not in result:
                result["message"] = "Some cell values were truncated to fit size limits."
        if total_available == -1:
            result["truncated"] = True
            result["message"] = f"Results truncated at {max_rows} rows. Increase max_rows or add LIMIT to your query."

        result_size = len(json.dumps(result, default=str))
        if result_size > _MAX_RESPONSE_BYTES:
            while len(data) > 1 and len(json.dumps({"columns": columns, "rows": data}, default=str)) > _MAX_RESPONSE_BYTES:
                data = data[: len(data) // 2]
            if len(data) <= 1 and len(json.dumps({"columns": columns, "rows": data}, default=str)) > _MAX_RESPONSE_BYTES:
                return {
                    "error": "Single row exceeds response size limit.",
                    "columns": columns,
                    "row_count": 0,
                    "rows": [],
                }
            result["rows"] = data
            result["row_count"] = len(data)
            result["truncated"] = True
            result["message"] = f"Response truncated to {len(data)} rows to fit {_MAX_RESPONSE_BYTES // 1_000_000}MB limit."

        return result
    except Exception as exc:
        logger.exception("execute_sql failed")
        return {"error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


# =====================================================================
#  Sync Tools (3)
# =====================================================================


@mcp.tool()
def trigger_sync(
    repo: str = "vllm-project/vllm",
    mode: str = "incremental",
    labels: str = "",
    target: str = "",
) -> dict:
    """Trigger a GitHub sync for a specific repository.

    Starts sync_github.py in the background. Use sync_status() to monitor progress.

    Args:
        repo: GitHub repo, e.g. "vllm-project/vllm" or "sgl-project/sglang".
        mode: "incremental" (since last sync), "full" (all issues), or "targeted" (specific issues).
        labels: Comma-separated label filter, e.g. "rocm,bug". Empty = all issues.
        target: Comma-separated issue/PR numbers for targeted mode, e.g. "39303,39616". Required when mode="targeted".

    Returns:
        {status, pid, repo, mode, message}
    """
    global _running_proc

    try:
        _reap_finished_sync()

        if _running_proc and _running_proc.poll() is None:
            return {
                "status": "already_running",
                "pid": _running_proc.pid,
                "message": "A sync is already in progress. Use sync_status() to check.",
            }

        if not _REPO_PATTERN.match(repo):
            return {"error": f"Invalid repo name: {repo}"}

        allowed = _load_allowed_repos()
        if not allowed:
            return {"error": "Cannot load repo config; sync refused (fail-closed)."}
        if repo not in allowed:
            return {
                "error": f"Repository '{repo}' is not in the allowed list. "
                         f"Allowed: {sorted(allowed)}",
            }

        if mode not in ("incremental", "full", "targeted"):
            return {"error": f"Invalid mode: {mode}. Must be incremental/full/targeted."}

        if mode == "targeted" and not target.strip():
            return {"error": "mode='targeted' requires the 'target' parameter with issue/PR numbers."}

        probe_fd = _acquire_sync_lock()
        if probe_fd is None:
            return {"status": "locked", "message": "Another sync is running (external lock held)."}
        _release_sync_lock(probe_fd)

        log_path = _PROJ / "data" / f"sync_mcp_{repo.replace('/', '_')}.log"

        cmd = [
            "flock", "-n", _SYNC_LOCK_FILE, "--",
            str(VENV_PYTHON),
            str(SYNC_SCRIPT),
            "--repo", repo,
            "--labels", labels,
            "--mode", mode,
            "--include-comments",
            "--max-comments", "100",
            "--log-level", "WARNING",
        ]
        if mode == "targeted" and target.strip():
            cmd.extend(["--target", target.strip()])

        with open(log_path, "a") as logf:
            _running_proc = subprocess.Popen(
                cmd, stdout=logf, stderr=logf, cwd=str(_PROJ),
            )

        logger.info("Started sync PID %s: %s mode=%s", _running_proc.pid, repo, mode)

        return {
            "status": "started",
            "pid": _running_proc.pid,
            "repo": repo,
            "mode": mode,
            "message": f"Sync started for {repo} ({mode}). Use sync_status() to monitor.",
        }
    except Exception as exc:
        logger.exception("trigger_sync failed")
        return {"error": str(exc)}


@mcp.tool()
def trigger_sync_all() -> dict:
    """Trigger incremental sync for ALL allowed repositories (serial).

    Syncs each repo from config/sources.yaml one by one, then runs
    WAL checkpoint + ANALYZE. Use sync_status() to monitor.

    Returns:
        {status, pid, repos, mode, message}
    """
    global _running_proc

    try:
        _reap_finished_sync()

        if _running_proc and _running_proc.poll() is None:
            return {
                "status": "already_running",
                "pid": _running_proc.pid,
                "message": "A sync is already in progress. Use sync_status() to check.",
            }

        allowed = _load_allowed_repos()
        if not allowed:
            return {"error": "No allowed repositories configured (config missing or empty)."}

        for repo in sorted(allowed):
            if not _REPO_PATTERN.match(repo):
                return {"error": f"Invalid repo name in config: {repo}"}

        probe_fd = _acquire_sync_lock()
        if probe_fd is None:
            return {"status": "locked", "message": "Another sync is running (external lock held)."}
        _release_sync_lock(probe_fd)

        repos_json = json.dumps(sorted(allowed))
        wrapper_code = (
            "import fcntl, os, subprocess, sys, sqlite3\n"
            f"fd = os.open('{_SYNC_LOCK_FILE}', os.O_WRONLY | os.O_CREAT, 0o644)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except (OSError, IOError):\n"
            "    print('Lock held by another process', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            f"repos = {repos_json}\n"
            "_failures = []\n"
            "for repo in repos:\n"
            "    ret = subprocess.run([\n"
            f'        "{VENV_PYTHON}", "{SYNC_SCRIPT}",\n'
            '        "--repo", repo, "--labels", "", "--mode", "incremental",\n'
            '        "--include-comments", "--max-comments", "100", "--log-level", "WARNING",\n'
            f'    ], cwd="{_PROJ}")\n'
            "    if ret.returncode != 0:\n"
            '        print(f"FAILED: {repo}", file=sys.stderr)\n'
            "        _failures.append(repo)\n"
            "    else:\n"
            '        print(f"OK: {repo}")\n'
            f'c = sqlite3.connect("{DB_PATH}")\n'
            'c.execute("PRAGMA busy_timeout=30000")\n'
            'c.execute("PRAGMA wal_checkpoint(PASSIVE)")\n'
            'c.execute("ANALYZE")\n'
            "c.close()\n"
            "if _failures:\n"
            '    print(f"Failed repos: {\', \'.join(_failures)}", file=sys.stderr)\n'
            "    sys.exit(1)\n"
        )

        log_path = _PROJ / "data" / "sync_mcp_all.log"

        with open(log_path, "a") as logf:
            _running_proc = subprocess.Popen(
                [str(VENV_PYTHON), "-c", wrapper_code],
                stdout=logf,
                stderr=logf,
                cwd=str(_PROJ),
            )

        logger.info(
            "Started serial incremental sync PID %s for %d repos: %s",
            _running_proc.pid, len(allowed), sorted(allowed),
        )

        return {
            "status": "started",
            "pid": _running_proc.pid,
            "repos": sorted(allowed),
            "mode": "incremental",
            "message": f"Serial incremental sync started for {len(allowed)} repos. Use sync_status() to monitor.",
        }
    except Exception as exc:
        logger.exception("trigger_sync_all failed")
        return {"error": str(exc)}


@mcp.tool()
def sync_status() -> dict:
    """Check current sync process status and recent sync history.

    Returns:
        {current: {status, pid?}, recent_runs: [...], db: {signals, comments}}
    """
    global _running_proc

    try:
        result: dict = {}

        if _running_proc is not None:
            rc = _running_proc.poll()
            if rc is None:
                result["current"] = {"status": "running", "pid": _running_proc.pid}
            else:
                result["current"] = {"status": "finished", "exit_code": rc}
                _reap_finished_sync()
        else:
            result["current"] = {"status": "idle"}

        if DB_PATH.exists():
            conn = _get_repo().connection
            runs = conn.execute(
                "SELECT id, source_repo, status, started_at, completed_at, "
                "signals_total, signals_created, signals_updated, "
                "comments_fetched, api_calls_used, error_message "
                "FROM sync_runs ORDER BY started_at DESC LIMIT 5"
            ).fetchall()
            result["recent_runs"] = [dict(r) for r in runs]

            stats = conn.execute(
                "SELECT COUNT(*) as signals, "
                "(SELECT COUNT(*) FROM signal_comments) as comments "
                "FROM signals"
            ).fetchone()
            result["db"] = dict(stats)

        return result
    except Exception as exc:
        logger.exception("sync_status failed")
        return {"error": str(exc)}


# =====================================================================
#  Management Tools (2)
# =====================================================================


@mcp.tool()
def db_health() -> dict:
    """Database health check: size, WAL size, signal count, FTS5 integrity, last sync.

    Returns:
        {db_size_mb, wal_size_mb, signal_count, comment_count, fts_integrity, last_sync}
    """
    try:
        db_size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 1) if DB_PATH.exists() else 0

        wal_path = DB_PATH.parent / (DB_PATH.name + "-wal")
        wal_size_mb = round(wal_path.stat().st_size / (1024 * 1024), 1) if wal_path.exists() else 0

        conn = _get_repo().connection

        signal_count = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        comment_count = conn.execute("SELECT COUNT(*) AS n FROM signal_comments").fetchone()["n"]

        try:
            conn.execute(
                "INSERT INTO signals_fts(signals_fts, rank) VALUES('integrity-check', 1)"
            )
            conn.commit()
            fts_integrity = "ok"
        except sqlite3.OperationalError as fts_exc:
            fts_integrity = str(fts_exc)
        finally:
            conn.rollback()

        last_sync_row = conn.execute(
            "SELECT id, source_repo, status, started_at, completed_at "
            "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        last_sync = dict(last_sync_row) if last_sync_row else None

        return {
            "db_size_mb": db_size_mb,
            "wal_size_mb": wal_size_mb,
            "signal_count": signal_count,
            "comment_count": comment_count,
            "fts_integrity": fts_integrity,
            "last_sync": last_sync,
        }
    except Exception as exc:
        logger.exception("db_health failed")
        return {"error": str(exc)}


@mcp.tool()
def db_maintain(
    action: str = "analyze",
) -> dict:
    """Run a database maintenance action.

    Args:
        action: One of:
            - "analyze": Run ANALYZE to update query planner statistics.
            - "optimize_fts": Run FTS5 optimize to merge b-tree segments for faster searches.
            - "checkpoint": Force a WAL checkpoint (TRUNCATE mode) to shrink the WAL file.
            - "reconnect": Close current DB connections and clear singletons.
              Next query auto-rebuilds fresh connections. Use after sync to
              release stale WAL snapshots that block checkpoint truncation.

    Returns:
        {action, status, message}
    """
    try:
        if action not in ("analyze", "optimize_fts", "checkpoint", "reconnect"):
            return {"error": f"Invalid action: {action}. Must be analyze/optimize_fts/checkpoint/reconnect."}

        conn = _get_repo().connection

        if action == "analyze":
            conn.execute("ANALYZE")
            return {"action": action, "status": "ok", "message": "ANALYZE completed — query planner stats updated."}

        if action == "optimize_fts":
            try:
                conn.execute("INSERT INTO signals_fts(signals_fts) VALUES('optimize')")
                conn.commit()
            finally:
                conn.rollback()
            return {"action": action, "status": "ok", "message": "FTS5 optimize completed — b-tree segments merged."}

        if action == "checkpoint":
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            busy, log, checkpointed = result["busy"], result["log"], result["checkpointed"]
            return {
                "action": action,
                "status": "ok",
                "busy": busy,
                "wal_pages_total": log,
                "wal_pages_checkpointed": checkpointed,
                "message": "WAL checkpoint (TRUNCATE) completed.",
            }

        if action == "reconnect":
            global _repo_instance, _search_instance
            if _repo_instance is not None:
                _repo_instance.close()
            _repo_instance = None
            _search_instance = None
            return {
                "action": action,
                "status": "reconnected",
                "message": "Connections closed. Next query will open fresh connections.",
            }

        return {"error": "unreachable"}
    except Exception as exc:
        logger.exception("db_maintain failed")
        return {"error": str(exc)}


# =====================================================================
#  Main
# =====================================================================


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Signals MCP server")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = mcp.streamable_http_app()

    logger.info(
        "Starting Signals MCP server on %s:%s (db=%s)",
        args.host, args.port, DB_PATH,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
