"""
MCP dbhub integration tests — HTTP mode + JSON-RPC.

Starts dbhub as a subprocess in HTTP transport mode, then exercises
all 6 registered MCP tools (2 built-in + 4 custom) via JSON-RPC over
``/mcp``.

Skip conditions:
  - Node v24 binary missing
  - @bytebase/dbhub not installed
  - signals.db not found

Run:
    .venv/bin/python -m pytest tests/test_mcp_dbhub.py -v
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HARDCODED_NODE = "/home/yaywang/.nvm/versions/node/v24.14.0/bin/node"
_HARDCODED_ENTRY = "/home/yaywang/.nvm/versions/node/v24.14.0/lib/node_modules/@bytebase/dbhub/dist/index.js"


def _find_node() -> Path:
    """Find Node.js >= 24 binary."""
    env = os.environ.get("DBHUB_NODE")
    if env:
        return Path(env)
    which = shutil.which("node")
    if which:
        return Path(which)
    return Path(_HARDCODED_NODE)


def _find_dbhub_entry() -> Path:
    """Find dbhub entry point."""
    env = os.environ.get("DBHUB_ENTRY")
    if env:
        return Path(env)
    try:
        result = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            candidate = Path(result.stdout.strip()) / "@bytebase/dbhub/dist/index.js"
            if candidate.exists():
                return candidate
    except Exception:
        pass
    return Path(_HARDCODED_ENTRY)


DBHUB_NODE = _find_node()
DBHUB_ENTRY = _find_dbhub_entry()
DBHUB_TOML = Path(__file__).parent.parent / "dbhub.toml"
SIGNALS_DB = Path(__file__).parent.parent / "data/signals.db"

pytestmark = pytest.mark.mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


_CALL_ID = 0


def _mcp_request(base_url: str, method: str, params: dict | None = None):
    """Send a JSON-RPC request to the MCP Streamable HTTP endpoint.

    The MCP spec requires Accept to include both application/json and
    text/event-stream. The server may respond with either content type;
    we handle both.
    """
    global _CALL_ID
    _CALL_ID += 1
    resp = httpx.post(
        f"{base_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": _CALL_ID,
            "method": method,
            "params": params or {},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )

    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        # Parse SSE: look for the last "data:" line containing JSON
        data = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                import json as _json
                data = _json.loads(line[len("data:"):].strip())
        if data is None:
            return None, {"code": -1, "message": "No data in SSE response"}
    else:
        data = resp.json()

    if "error" in data:
        return None, data["error"]
    return data.get("result"), None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dbhub_url():
    """Start dbhub in HTTP mode, yield base URL, kill on teardown."""
    if not DBHUB_NODE.exists():
        pytest.skip(f"Node not found: {DBHUB_NODE}")
    if not DBHUB_ENTRY.exists():
        pytest.skip(f"dbhub not installed: {DBHUB_ENTRY}")
    if not SIGNALS_DB.exists():
        pytest.skip("signals.db not found")

    port = _free_port()
    env = {
        **os.environ,
        "PATH": f"{DBHUB_NODE.parent}:/usr/local/bin:/usr/bin:/bin",
    }
    proc = subprocess.Popen(
        [
            str(DBHUB_NODE),
            str(DBHUB_ENTRY),
            "--transport", "http",
            "--port", str(port),
            "--config", str(DBHUB_TOML),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if not _wait_for_port(port):
        out = proc.stderr.read().decode()[:500]
        proc.kill()
        pytest.skip(f"dbhub failed to start: {out}")

    yield f"http://localhost:{port}"
    proc.kill()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def mcp(dbhub_url):
    """Initialize MCP session and return a tool-call helper."""
    result, err = _mcp_request(dbhub_url, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1.0"},
    })
    assert err is None, f"initialize failed: {err}"

    def call_tool(name: str, arguments: dict | None = None):
        return _mcp_request(
            dbhub_url, "tools/call", {"name": name, "arguments": arguments or {}}
        )

    call_tool._base_url = dbhub_url  # type: ignore[attr-defined]
    call_tool._list_tools = lambda: _mcp_request(dbhub_url, "tools/list", {})  # type: ignore[attr-defined]
    return call_tool


# ===================================================================
# Tests
# ===================================================================

class TestMCPDbhub:
    """All 6 MCP tools + edge cases."""

    # MCP-01: tools/list -------------------------------------------------
    def test_mcp01_tools_list(self, mcp):
        result, err = mcp._list_tools()
        assert err is None, f"tools/list failed: {err}"
        tools = result.get("tools", [])
        names = {t["name"] for t in tools}
        expected = {
            "execute_sql",
            "search_objects",
            "search_signals",
            "get_signal_detail",
            "get_signal_changes",
            "get_gap_signals",
        }
        assert expected == names, (
            f"missing: {expected - names}, extra: {names - expected}"
        )

    # MCP-02: execute_sql — COUNT ----------------------------------------
    def test_mcp02_execute_sql_count(self, mcp):
        result, err = mcp("execute_sql", {"sql": "SELECT COUNT(*) AS cnt FROM signals"})
        assert err is None, f"execute_sql failed: {err}"
        text = result["content"][0]["text"]
        assert "cnt" in text

    # MCP-03: search_signals — FTS5 --------------------------------------
    def test_mcp03_search_signals(self, mcp):
        result, err = mcp("search_signals", {"query": "aiter MLA", "limit": 3})
        assert err is None, f"search_signals failed: {err}"
        text = result["content"][0]["text"]
        assert "signal_id" in text or "title" in text

    # MCP-04: get_signal_detail ------------------------------------------
    def test_mcp04_get_signal_detail(self, mcp):
        result, err = mcp(
            "get_signal_detail",
            {"signal_id": "github:vllm-project/vllm:issue:39303"},
        )
        assert err is None, f"get_signal_detail failed: {err}"
        text = result["content"][0]["text"]
        assert "39303" in text
        assert "comments_json" in text or "comment" in text.lower()

    # MCP-05: get_signal_changes -----------------------------------------
    def test_mcp05_get_signal_changes(self, mcp):
        result, err = mcp(
            "get_signal_changes",
            {"signal_id": "github:vllm-project/vllm:issue:39303", "limit": 5},
        )
        assert err is None, f"get_signal_changes failed: {err}"
        text = result["content"][0]["text"]
        assert "change_type" in text or "new_signal" in text

    # MCP-06: get_gap_signals — nonexistent gap --------------------------
    def test_mcp06_gap_signals_empty(self, mcp):
        result, err = mcp("get_gap_signals", {"gap_id": "nonexistent_gap", "limit": 5})
        assert err is None, f"get_gap_signals should not error on empty result: {err}"

    # MCP-07: search_objects — tables ------------------------------------
    def test_mcp07_search_objects_tables(self, mcp):
        result, err = mcp("search_objects", {"object_type": "table"})
        assert err is None, f"search_objects failed: {err}"
        text = result["content"][0]["text"]
        assert "signals" in text

    # MCP-08: execute_sql — readonly rejects DROP ------------------------
    def test_mcp08_readonly_rejects_drop(self, mcp):
        result, err = mcp("execute_sql", {"sql": "DROP TABLE signals"})
        combined = str(err) + str(result)
        assert any(
            kw in combined.lower()
            for kw in ("error", "not allowed", "readonly", "read-only", "not authorized")
        ), f"DROP should be rejected, got: result={result}, err={err}"

    # MCP-09: execute_sql — FTS5 special chars ---------------------------
    @pytest.mark.parametrize("query", ["C++", "[ROCm]", "MI300X/MI355X"])
    def test_mcp09_fts_special_chars(self, mcp, query):
        sql = (
            "SELECT signal_id, title FROM signals s "
            "JOIN signals_fts f ON s.rowid=f.rowid "
            f"WHERE signals_fts MATCH '{query}' LIMIT 3"
        )
        result, err = mcp("execute_sql", {"sql": sql})
        assert err is None or "fts5" in str(err).lower(), (
            f"Unexpected error for query '{query}': {err}"
        )

    # MCP-10: execute_sql — cross-repo aggregation -----------------------
    def test_mcp10_cross_repo_query(self, mcp):
        result, err = mcp(
            "execute_sql",
            {"sql": "SELECT source_repo, COUNT(*) AS cnt FROM signals GROUP BY source_repo"},
        )
        assert err is None, f"cross-repo query failed: {err}"
        text = result["content"][0]["text"]
        assert "vllm" in text
        assert "sglang" in text
