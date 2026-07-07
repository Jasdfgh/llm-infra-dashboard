# Signal Ingestion — AI Infra Gap Intelligence

> **Module 1+3:** Ingest, store, and serve GitHub signals (issues, PRs, comments) for downstream classification and gap analysis.

## Overview

This module continuously collects and indexes public GitHub activity from key AI-infrastructure repositories, storing it in a local SQLite database optimized for full-text search. It exposes **12 MCP tools** for querying, syncing, and maintaining the dataset — letting any MCP-capable agent (Cursor, Claude Code, etc.) search across ~69 K signals and ~257 K comments in milliseconds.

Current coverage: **vllm-project/vllm** and **sgl-project/sglang**. Adding a new repo requires only a one-line edit to `config/sources.yaml`.

## Quick Start

### Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.13+ | Core runtime |
| Node.js | v24+ | dbhub MCP server |

### Installation

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/init_db.py
```

Copy `.env.example` to `.env` and fill in your GitHub token(s).

### Start Services

```bash
# Signals MCP service (12 tools, default port 8082)
.venv/bin/python scripts/signals_mcp_server.py --host 0.0.0.0

# dbhub MCP service (read-only SQL, default port 8081)
bash scripts/start_dbhub_server.sh
```

### Connect from Cursor / Claude Code

Add to your MCP config (`~/.cursor/mcp.json` or `~/.claude.json`):

```json
{
  "mcpServers": {
    "signals-service": {
      "url": "http://<server-ip>:8082/mcp"
    },
    "signals-db": {
      "url": "http://<server-ip>:8081/mcp"
    }
  }
}
```

Reload the editor and you are ready to go. See [MCP Service Guide](examples/mcp_service_guide.md) for the full 12-tool reference with examples.

## Directory Structure

| Directory | Contents |
|---|---|
| `src/` | Core library — ingestion, storage, sync, search |
| `scripts/` | CLI tools, MCP servers, sync runners, ops helpers |
| `tests/` | pytest suite (17 test files) |
| `config/` | `sources.yaml` — tracked repos, rate limits, schedules |
| `examples/` | MCP guide, downstream integration samples, ops cheatsheet |

## Data Sources

Tracked repositories are defined in `config/sources.yaml`. Currently active:

- `vllm-project/vllm` (P0)
- `sgl-project/sglang` (P0)

To add a new repo, append an entry to the `repos` list in `sources.yaml` — no code changes required. Several additional repos (pytorch, triton, ollama, etc.) are pre-configured but commented out.

## Sync

| Method | Command / Tool |
|---|---|
| **Automatic** | systemd timer — incremental every 2 hours |
| **Manual CLI** | `bash scripts/incremental_sync.sh` |
| **MCP tool** | `trigger_sync` (single repo) / `trigger_sync_all` (all repos) |

Use the `sync_status` MCP tool or `bash scripts/signals_status.sh` to check progress.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

## Operations

```bash
bash scripts/signals_status.sh      # one-command system health check
```

See [Ops Cheatsheet](examples/ops_cheatsheet.md) for common maintenance recipes.

## Documentation

| Doc | Description |
|---|---|
| [MCP Service Guide](examples/mcp_service_guide.md) | 12 MCP tools with parameter tables and usage examples |
| [Module 2 Quickstart](examples/module2_quickstart.py) | Classifier integration sample |
| [Module 4 Agent Queries](examples/module4_agent_queries.py) | Agent query examples for gap analysis |
