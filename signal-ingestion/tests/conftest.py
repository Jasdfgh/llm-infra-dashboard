"""Shared pytest configuration and custom markers."""

import sys
from pathlib import Path

_WORKSHOP = Path(__file__).resolve().parent.parent
if str(_WORKSHOP) not in sys.path:
    sys.path.insert(0, str(_WORKSHOP))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: requires real GitHub API access (deselected by default)",
    )
    config.addinivalue_line(
        "markers",
        "mcp: requires dbhub MCP server (Node v24 + npm install -g @bytebase/dbhub)",
    )
    config.addinivalue_line(
        "markers",
        "e2e_agent: end-to-end Agent tests requiring Cursor/Claude CLI (TODO)",
    )
