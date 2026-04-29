#!/bin/bash
# ═══════════════════════════════════════════════════════════
# ops_cheatsheet.sh — 日常运维速查
# 不需要执行这个文件，只是参考。复制你需要的命令即可。
# ═══════════════════════════════════════════════════════════

# ── 初始化（只需一次）────────────────────────────────────
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxx" > .env
.venv/bin/python scripts/init_db.py

# ══════════════════════════════════════════════════════════
# 手动同步 — 任何人都可以手动触发
# ══════════════════════════════════════════════════════════

# ── 日常增量（最常用，只拉上次以来的变化）──
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --include-comments
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd --include-comments

# ── 全量（从头拉，首次或想重建时用）──
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full --include-comments
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd --mode full --include-comments

# ── 按日期拉某段时间 ──
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --since 2026-04-01T00:00:00Z --include-comments
# 注意：since 过滤的是 updated_at >= since，不是 created_at。
# 一条 2023 年创建的 issue，如果 4/1 之后有人评论了，也会被返回。

# ── 不限 label（拉全部 issue/PR）──
# 注意：大 repo 可能有数万条，建议加 --since 限制时间范围
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels "" --since 2026-04-01T00:00:00Z --include-comments

# ── 只拉特定 issue/PR（调试/追踪用）──
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --mode targeted --target 39303,39616 --include-comments

# ══════════════════════════════════════════════════════════
# 定时同步 — systemd timer（已配置，自动运行）
# ══════════════════════════════════════════════════════════
# 当前已通过 systemd user timer 自动化，不需要 crontab：
#   signals-sync.timer        — 每 2h 增量同步（调 incremental_sync.sh）
#   signals-logrotate.timer   — 每日日志轮转
#
# 查看状态：
systemctl --user status signals-sync.timer
systemctl --user list-timers

# 手动触发增量同步：
bash scripts/incremental_sync.sh

# 手动触发全量同步（耗时较长）：
# nohup bash scripts/full_sync_and_report.sh > data/full_sync_master.log 2>&1 &

# 通过 MCP 触发同步（在 Cursor 里让 Agent 执行）：
#   trigger_sync(repo="vllm-project/vllm", mode="incremental")
#   trigger_sync_all()
#   sync_status()

# ══════════════════════════════════════════════════════════
# Vivi (Module 2) 的典型工作流
# ══════════════════════════════════════════════════════════

# 1. 确认同步已跑完
sqlite3 data/signals.db "SELECT id, status, completed_at FROM sync_runs ORDER BY started_at DESC LIMIT 3"

# 2. 看有多少未分类的 signal
.venv/bin/python -c "
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch
with SignalRepository('data/signals.db') as repo:
    feed = SignalSearch(repo).get_feed(since='2000-01-01T00:00:00Z', classified=False)
    print(f'未分类 signal: {feed[\"pagination\"][\"total\"]}')
"

# 3. 跑 quickstart 做分类（替换 your_classifier() 为你的 LLM 分类器）
.venv/bin/python examples/module2_quickstart.py

# ══════════════════════════════════════════════════════════
# Zijun (Module 4) 的典型查询
# ══════════════════════════════════════════════════════════

# ── FTS5 搜索 ──
.venv/bin/python scripts/search_signals.py --query "aiter MLA"
.venv/bin/python scripts/search_signals.py --query "speculative decoding AMD"
.venv/bin/python scripts/search_signals.py --query "ROCm" --since 2026-04-01T00:00:00Z --limit 50

# ── 按 author 过滤（找 NVIDIA 员工的贡献）──
# author 不是 CLI 参数，用 --json + jq 或直接 SQL
.venv/bin/python scripts/search_signals.py --query "ROCm" --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for s in data.get('results', []):
    if 'nvidia' in (s.get('author') or '').lower():
        print(f'  #{s[\"source_number\"]} by {s[\"author\"]}: {s[\"title\"][:70]}')
"

# 或者直接 SQL（更灵活）
sqlite3 data/signals.db "SELECT source_number, author, SUBSTR(title,1,70) FROM signals WHERE LOWER(author) LIKE '%nvidia%' ORDER BY updated_at DESC LIMIT 10"

# ── 找 AMD 员工的贡献 ──
sqlite3 data/signals.db "SELECT source_number, author, SUBSTR(title,1,70) FROM signals WHERE LOWER(author) LIKE '%-amd%' ORDER BY updated_at DESC LIMIT 10"

# ── 查看某条 signal 的完整详情 ──
.venv/bin/python scripts/search_signals.py --detail github:vllm-project/vllm:issue:39303

# ── 查变更历史 ──
.venv/bin/python scripts/show_changes.py --since 2026-04-24T00:00:00Z
.venv/bin/python scripts/show_changes.py --signal-id github:vllm-project/vllm:issue:39303

# ── 跨 repo 搜索（同时搜 vllm + sglang）──
.venv/bin/python scripts/search_signals.py --query "Eagle3 speculative"

# ── 只看某个 repo ──
.venv/bin/python scripts/search_signals.py --repo sgl-project/sglang --state open --limit 20

# ══════════════════════════════════════════════════════════
# MCP 服务（给 Agent 用）
# ══════════════════════════════════════════════════════════

# Signals Service MCP — 12 个工具，systemd 常驻，端口 8082
# 完整接入教程见 examples/mcp_service_guide.md

# ── 查看服务状态 ──
bash scripts/signals_status.sh
systemctl --user status signals-sync-mcp.service
systemctl --user status signals-dbhub.service

# ── 重启服务（代码更新后需要）──
systemctl --user restart signals-sync-mcp.service
systemctl --user restart signals-dbhub.service

# ── Cursor/Claude Code MCP 配置 ──
# 在 ~/.cursor/mcp.json 的 mcpServers 中添加：
#   同机器：  "signals-service": { "url": "http://localhost:8082/mcp" }
#   内网：    "signals-service": { "url": "http://<server-ip>:8082/mcp" }
#
# dbhub（后备只读 SQL 通道，端口 8081）：
#   "signals-dbhub": { "url": "http://<server-ip>:8081/mcp" }

# ══════════════════════════════════════════════════════════
# 代码文件实时读取（不存到 DB，按需读）
# ══════════════════════════════════════════════════════════

# 在 Cursor 里通过 GitHub MCP 直接读代码文件：
#   get_file_contents(owner="vllm-project", repo="vllm",
#       path="vllm/v1/attention/backends/mla/rocm_aiter_mla.py")
#
# 也可以指定历史版本（某个 commit/tag）：
#   get_file_contents(owner="vllm-project", repo="vllm",
#       path="vllm/v1/attention/backends/mla/rocm_aiter_mla.py",
#       branch="v0.8.0")  # 某个 tag
#
# 或者 CLI：
# curl -H "Authorization: Bearer $GITHUB_TOKEN" \
#   "https://api.github.com/repos/vllm-project/vllm/contents/vllm/v1/attention/backends/mla/rocm_aiter_mla.py?ref=v0.8.0"

# ══════════════════════════════════════════════════════════
# 数据库诊断
# ══════════════════════════════════════════════════════════

# 总览
.venv/bin/python -c "
import sqlite3, json
c = sqlite3.connect('data/signals.db')
for t in ['signals','signal_comments','signal_changes','signal_refs','sync_runs']:
    print(f'{t}: {c.execute(\"SELECT COUNT(*) FROM \"+t).fetchone()[0]}')
# 按 repo 分
for r in c.execute('SELECT source_repo, COUNT(*) FROM signals GROUP BY source_repo'):
    print(f'  {r[0]}: {r[1]}')
"

# 同步历史
sqlite3 data/signals.db "SELECT id, status, signals_total, completed_at FROM sync_runs ORDER BY started_at DESC LIMIT 5"

# API 配额检查
.venv/bin/python -c "
import asyncio, httpx, os; from dotenv import load_dotenv; load_dotenv()
async def m():
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get('https://api.github.com/rate_limit', headers={'Authorization': f'Bearer {os.getenv(\"GITHUB_PERSONAL_ACCESS_TOKEN\")}'})
        d = r.json()['resources']
        print(f'REST: {d[\"core\"][\"remaining\"]}/{d[\"core\"][\"limit\"]}  Search: {d[\"search\"][\"remaining\"]}/{d[\"search\"][\"limit\"]}')
asyncio.run(m())
"

# ══════════════════════════════════════════════════════════
# 测试
# ══════════════════════════════════════════════════════════
.venv/bin/python -m pytest tests/ -q -k "not network and not mcp_dbhub and not e2e_agent"  # 全部（~340 case, ~10min）
.venv/bin/python -m pytest tests/test_smoke_downstream.py -v   # 只跑业务冒烟
.venv/bin/python -m pytest tests/test_smoke_real_data.py -v    # 真实数据 fuzz

# ══════════════════════════════════════════════════════════
# 添加新 repo（零代码改动）
# ══════════════════════════════════════════════════════════
# 1. 直接跑 sync（不需要改任何代码或配置文件）：
.venv/bin/python scripts/sync_github.py --repo ROCm/aiter --labels "" --mode full --include-comments
# 2. 加到 config/sources.yaml（systemd timer 和 MCP trigger_sync_all 会自动包含）
# 3. 重启 MCP 服务使配置生效：systemctl --user restart signals-sync-mcp.service
