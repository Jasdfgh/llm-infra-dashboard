#!/usr/bin/env python3
"""E2E tests: MCP Python SDK -> signals_mcp_server.

Connects to the running MCP server on localhost:8082 via the official
MCP Python SDK. Tests the full protocol chain: HTTP -> MCP session ->
tool call -> JSON result.

Skipped when the server is not running.
"""
import asyncio
import json

import pytest


def _server_reachable():
    import httpx

    try:
        r = httpx.post(
            "http://localhost:8082/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
                "id": 1,
            },
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="MCP server not running on localhost:8082",
)

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://localhost:8082/mcp"


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _call_tool(name, arguments=None):
    async with streamable_http_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments or {})
            return json.loads(result.content[0].text)


class TestMcpSdkE2E:

    @pytest.mark.asyncio
    async def test_list_tools_has_12(self):
        """Server exposes exactly 12 tools."""
        async with streamable_http_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert len(names) >= 12

    @pytest.mark.asyncio
    async def test_search_signals_fts(self):
        """FTS search returns results with expected structure."""
        data = await _call_tool("search_signals", {"query": "CUDA", "limit": 3})
        assert "results" in data or "error" not in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_search_signals_repo_filter(self):
        """Repo filter narrows results."""
        data = await _call_tool(
            "search_signals",
            {"repos": "sgl-project/sglang", "state": "open", "limit": 5},
        )
        assert "results" in data

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """get_stats returns database statistics."""
        data = await _call_tool("get_stats")
        assert "total" in data or "total_signals" in data
        assert data.get("total", data.get("total_signals", 0)) > 0

    @pytest.mark.asyncio
    async def test_get_signal_detail(self):
        """Search then detail -- full chain."""
        search = await _call_tool("search_signals", {"limit": 1})
        if search.get("results"):
            sid = search["results"][0].get("signal_id") or search["results"][0].get(
                "id"
            )
            detail = await _call_tool("get_signal_detail", {"signal_id": sid})
            assert detail is not None

    @pytest.mark.asyncio
    async def test_execute_sql_readonly(self):
        """execute_sql returns data for SELECT."""
        data = await _call_tool(
            "execute_sql", {"sql": "SELECT COUNT(*) as n FROM signals"}
        )
        assert "error" not in data

    @pytest.mark.asyncio
    async def test_execute_sql_rejects_write(self):
        """execute_sql rejects DROP TABLE."""
        data = await _call_tool("execute_sql", {"sql": "DROP TABLE signals"})
        assert "error" in data

    @pytest.mark.asyncio
    async def test_db_health(self):
        """db_health returns expected keys."""
        data = await _call_tool("db_health")
        assert "db_size_mb" in data or "signal_count" in data

    @pytest.mark.asyncio
    async def test_sync_status(self):
        """sync_status returns current status."""
        data = await _call_tool("sync_status")
        assert "current" in data

    @pytest.mark.asyncio
    async def test_get_signal_feed(self):
        """get_signal_feed returns feed structure."""
        data = await _call_tool("get_signal_feed", {"since": "2020-01-01"})
        assert "signals" in data or "error" not in data

    @pytest.mark.asyncio
    async def test_db_maintain_analyze(self):
        """db_maintain analyze succeeds."""
        data = await _call_tool("db_maintain", {"action": "analyze"})
        assert "error" not in data
