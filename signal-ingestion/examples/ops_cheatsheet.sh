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
# 定时同步 — crontab -e 添加以下行
# ══════════════════════════════════════════════════════════
# vllm 每 2 小时增量
# 0 */2 * * * cd /home/yaywang/my-llm-infra-dashboard && .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --include-comments >> data/sync.log 2>&1
#
# sglang 每 4 小时增量
# 0 */4 * * * cd /home/yaywang/my-llm-infra-dashboard && .venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd --include-comments >> data/sync.log 2>&1
#
# vllm 每日凌晨 2 点全量（catch up 可能漏掉的）
# 0 2 * * * cd /home/yaywang/my-llm-infra-dashboard && .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full --include-comments >> data/sync.log 2>&1

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

# ── dbhub 首次安装（只需一次）──
# 需要 Node.js >= 24 和 C++ 编译工具链
# sudo apt install build-essential python3  # 如果没有 gcc/make
export PATH="/home/yaywang/.nvm/versions/node/v24.14.0/bin:$PATH"
npm install -g @bytebase/dbhub@latest

# ── 验证 dbhub 能连接 signals.db ──
dbhub --transport http --port 8080 --config dbhub.toml
# 浏览器打开 http://localhost:8080 看到 Workbench UI = 成功
# Ctrl+C 停止

# ── Cursor 配置（已配好，重启 Cursor 即可）──
# 配置在 ~/.cursor/mcp.json 的 "signals-db" 段
# 注意：command 必须是 node 绝对路径，不能用 npx（会走 Cursor 内置 v20）
# 6 个工具：search_signals / get_signal_detail / get_signal_changes / get_gap_signals / execute_sql / search_objects

# 远程 HTTP 模式（Vivi/Zijun 在其他机器上用任何 MCP client 连）
# npx @bytebase/dbhub@latest --transport http --port 8080 --config dbhub.toml
# 然后 MCP client 连 http://<your-ip>:8080/mcp

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
.venv/bin/python -m pytest tests/ -q -k "not network"         # 全部（~247 case, 16s）
.venv/bin/python -m pytest tests/test_smoke_downstream.py -v   # 只跑业务冒烟
.venv/bin/python -m pytest tests/test_smoke_real_data.py -v    # 真实数据 fuzz

# ══════════════════════════════════════════════════════════
# 添加新 repo（零代码改动）
# ══════════════════════════════════════════════════════════
# 1. 直接跑 sync（不需要改任何代码或配置文件）：
.venv/bin/python scripts/sync_github.py --repo ROCm/aiter --labels "" --mode full --include-comments
# 2. （可选）加到 config/sources.yaml 做记录
# 3. 加到 crontab 定时拉
