"""Shared pytest configuration and custom markers."""


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
