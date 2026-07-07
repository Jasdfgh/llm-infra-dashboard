# Ops Cheatsheet — Daily Operations Quick Reference

Copy-paste the commands you need. All commands assume you are in the project root.

## Initialization (one-time setup)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxx" >> .env
.venv/bin/python scripts/init_db.py
```

## Manual Sync

### Daily incremental (most common, fetches changes since last sync)

```bash
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --include-comments
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd --include-comments
```

### Full sync (from scratch, for first run or rebuilds)

```bash
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full --include-comments
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd --mode full --include-comments
```

### Fetch by date range

```bash
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --since 2026-04-01T00:00:00Z --include-comments
```

> `--since` filters on `updated_at >= since`, not `created_at`.
> An issue created in 2023 will be returned if someone commented on it after 4/1.

### No label filter (fetch all issues/PRs)

```bash
# Large repos may have tens of thousands of items; add --since to limit the time range
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels "" --since 2026-04-01T00:00:00Z --include-comments
```

### Fetch specific issues/PRs (for debugging/tracking)

```bash
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --mode targeted --target 39303,39616 --include-comments
```

## Scheduled Sync — systemd timer

Currently automated via systemd user timers; no crontab needed:
- `signals-sync.timer` — incremental sync every 2h (calls `incremental_sync.sh`)

```bash
# Check status
systemctl --user status signals-sync.timer
systemctl --user list-timers

# Manually trigger incremental sync
bash scripts/incremental_sync.sh

# Manually trigger full sync (takes a long time)
# nohup bash scripts/full_sync_and_report.sh > data/full_sync_master.log 2>&1 &
```

Trigger sync via MCP (have the Agent execute in Cursor):

```
trigger_sync(repo="vllm-project/vllm", mode="incremental")
trigger_sync_all()
sync_status()
```

## Typical Workflow for Vivi (Module 2)

### 1. Verify sync has completed

```bash
sqlite3 data/signals.db "SELECT id, status, completed_at FROM sync_runs ORDER BY started_at DESC LIMIT 3"
```

### 2. Check how many unclassified signals exist

```bash
.venv/bin/python -c "
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch
with SignalRepository('data/signals.db') as repo:
    feed = SignalSearch(repo).get_feed(since='2000-01-01T00:00:00Z', classified=False)
    print(f'未分类 signal: {feed[\"pagination\"][\"total\"]}')
"
```

### 3. Run quickstart for classification

```bash
.venv/bin/python examples/module2_quickstart.py
```

## Typical Queries for Zijun (Module 4)

### FTS5 search

```bash
.venv/bin/python scripts/search_signals.py --query "aiter MLA"
.venv/bin/python scripts/search_signals.py --query "speculative decoding AMD"
.venv/bin/python scripts/search_signals.py --query "ROCm" --since 2026-04-01T00:00:00Z --limit 50
```

### Filter by author (find contributions from NVIDIA employees)

```bash
# author is not a CLI argument; use --json + jq or raw SQL
.venv/bin/python scripts/search_signals.py --query "ROCm" --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for s in data.get('results', []):
    if 'nvidia' in (s.get('author') or '').lower():
        print(f'  #{s[\"source_number\"]} by {s[\"author\"]}: {s[\"title\"][:70]}')
"

# Or use raw SQL (more flexible)
sqlite3 data/signals.db "SELECT source_number, author, SUBSTR(title,1,70) FROM signals WHERE LOWER(author) LIKE '%nvidia%' ORDER BY updated_at DESC LIMIT 10"
```

### Find contributions from AMD employees

```bash
sqlite3 data/signals.db "SELECT source_number, author, SUBSTR(title,1,70) FROM signals WHERE LOWER(author) LIKE '%-amd%' ORDER BY updated_at DESC LIMIT 10"
```

### View full details of a specific signal

```bash
.venv/bin/python scripts/search_signals.py --detail github:vllm-project/vllm:issue:39303
```

### View change history

```bash
.venv/bin/python scripts/show_changes.py --since 2026-04-24T00:00:00Z
.venv/bin/python scripts/show_changes.py --signal-id github:vllm-project/vllm:issue:39303
```

### Cross-repo search (search vllm + sglang simultaneously)

```bash
.venv/bin/python scripts/search_signals.py --query "Eagle3 speculative"
```

### Search within a single repo

```bash
.venv/bin/python scripts/search_signals.py --repo sgl-project/sglang --state open --limit 20
```

## MCP Service (for Agent use)

Signals Service MCP — 12 tools, systemd-managed, port 8082.
Full integration guide at `examples/mcp_service_guide.md`.

### Check service status

```bash
bash scripts/signals_status.sh
systemctl --user status signals-sync-mcp.service
systemctl --user status signals-dbhub.service
```

### Restart services (required after code updates)

```bash
systemctl --user restart signals-sync-mcp.service
systemctl --user restart signals-dbhub.service
```

### Cursor/Claude Code MCP configuration

Add to `mcpServers` in `~/.cursor/mcp.json`:

```jsonc
// Same machine
"signals-service": { "url": "http://localhost:8082/mcp" }
// LAN
"signals-service": { "url": "http://<server-ip>:8082/mcp" }

// dbhub (fallback read-only SQL channel, port 8081)
"signals-dbhub": { "url": "http://<server-ip>:8081/mcp" }
```

## Live Code File Reading (not stored in DB, fetched on demand)

Read code files directly via GitHub MCP in Cursor:

```
get_file_contents(owner="vllm-project", repo="vllm",
    path="vllm/v1/attention/backends/mla/rocm_aiter_mla.py")

# Historical version (commit/tag)
get_file_contents(owner="vllm-project", repo="vllm",
    path="vllm/v1/attention/backends/mla/rocm_aiter_mla.py",
    branch="v0.8.0")
```

Or via CLI:

```bash
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/vllm-project/vllm/contents/vllm/v1/attention/backends/mla/rocm_aiter_mla.py?ref=v0.8.0"
```

## Database Diagnostics

### Overview

```bash
.venv/bin/python -c "
import sqlite3, json
c = sqlite3.connect('data/signals.db')
for t in ['signals','signal_comments','signal_changes','signal_refs','sync_runs']:
    print(f'{t}: {c.execute(\"SELECT COUNT(*) FROM \"+t).fetchone()[0]}')
# By repo
for r in c.execute('SELECT source_repo, COUNT(*) FROM signals GROUP BY source_repo'):
    print(f'  {r[0]}: {r[1]}')
"
```

### Sync history

```bash
sqlite3 data/signals.db "SELECT id, status, signals_total, completed_at FROM sync_runs ORDER BY started_at DESC LIMIT 5"
```

### API rate limit check

```bash
.venv/bin/python -c "
import asyncio, httpx, os; from dotenv import load_dotenv; load_dotenv()
async def m():
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get('https://api.github.com/rate_limit', headers={'Authorization': f'Bearer {os.getenv(\"GITHUB_PERSONAL_ACCESS_TOKEN\")}'})
        d = r.json()['resources']
        print(f'REST: {d[\"core\"][\"remaining\"]}/{d[\"core\"][\"limit\"]}  Search: {d[\"search\"][\"remaining\"]}/{d[\"search\"][\"limit\"]}')
asyncio.run(m())
"
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q -k "not network and not mcp_dbhub and not e2e_agent"  # all (~340 cases, ~10min)
.venv/bin/python -m pytest tests/test_smoke_downstream.py -v   # downstream smoke tests only
.venv/bin/python -m pytest tests/test_smoke_real_data.py -v    # real data fuzz tests
```

## Add a New Repo (zero code changes)

```bash
# 1. Run sync directly (no code or config changes needed)
.venv/bin/python scripts/sync_github.py --repo ROCm/aiter --labels "" --mode full --include-comments
# 2. Add to config/sources.yaml (systemd timer and MCP trigger_sync_all will include it automatically)
# 3. Restart MCP service to apply config
systemctl --user restart signals-sync-mcp.service
```
