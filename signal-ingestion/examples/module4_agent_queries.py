#!/usr/bin/env python3
"""Module 4 (Agent Workflow) 查询示例 — Zijun 专用。

用法:
    .venv/bin/python examples/module4_agent_queries.py

本脚本演示 Agent 常见的 6 类查询（通过 Python API 直调）。

MCP 使用方式:
  本脚本演示 Python 直调接口。如果通过 MCP（dbhub）访问，
  相同功能对应以下工具：
    search_signals(query)        → 本脚本的 "搜索" 部分
    get_signal_detail(signal_id) → "详情" 部分
    get_signal_changes(signal_id) → "变更历史" 部分
    get_gap_signals(gap_id)      → "gap 关联" 部分
    execute_sql(sql)             → 任意只读 SQL

  MCP 配置见 dbhub.toml（项目根目录）。

MCP 使用方式（不需要 Python）：
  1. 确保 ~/.cursor/mcp.json 里有 signals-db 配置（见 dbhub.toml）
  2. 重启 Cursor
  3. 在对话中直接让 Agent 用以下工具：
     - search_signals(query="aiter MLA")
     - get_signal_detail(signal_id="github:vllm-project/vllm:issue:39303")
     - get_signal_changes(signal_id="...", limit=20)
     - get_gap_signals(gap_id="gap_001")
     - execute_sql(sql="SELECT COUNT(*) FROM signals")

远程 MCP（HTTP 模式，Vivi/Zijun 在其他机器上用）：
  npx @bytebase/dbhub@latest --transport http --port 8080 --config dbhub.toml
  然后 MCP client 连接 http://<your-ip>:8080/mcp

接口版本:
    from src import INTERFACE_VERSION  # pin 到 "1.0"

依赖: 仅 Python stdlib + src/ (无额外包)
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

# ─────────────────────────────────────────────────────────────
# 数据库路径
# ─────────────────────────────────────────────────────────────
_FIXTURE_DB = PROJECT_ROOT / "data" / "fixtures" / "signals_fixture.db"
_MAIN_DB = PROJECT_ROOT / "data" / "signals.db"

if _FIXTURE_DB.exists():
    DB_PATH = _FIXTURE_DB
elif _MAIN_DB.exists():
    DB_PATH = _MAIN_DB
else:
    print("❌ 找不到数据库文件。请先运行:")
    print("   .venv/bin/python scripts/init_db.py")
    print("   .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full")
    print()
    print(f"   期望路径: {_FIXTURE_DB}")
    print(f"         或: {_MAIN_DB}")
    sys.exit(1)

SEPARATOR = "=" * 60


def _print_signal_row(sig: dict, prefix: str = "  ") -> None:
    """打印 signal 摘要的单行格式。"""
    title = (sig.get("title") or "")[:70]
    state = sig.get("github_state") or "?"
    labels = sig.get("github_labels", [])
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except (json.JSONDecodeError, TypeError):
            labels = []
    label_str = ", ".join(labels[:3]) if labels else "-"
    print(f"{prefix}{sig['signal_id']}")
    print(f"{prefix}  title:  {title}")
    print(f"{prefix}  state:  {state}  labels: [{label_str}]")


def main() -> None:
    print(SEPARATOR)
    print("Module 4 Agent Queries — Python API 查询示例")
    print(SEPARATOR)
    print(f"数据库: {DB_PATH}")
    print()

    with SignalRepository(DB_PATH) as repo:
        search = SignalSearch(repo)

        # ── 场景 1: FTS5 全文搜索 ──────────────────────────
        print(f"{SEPARATOR}\n场景 1: FTS5 搜索 (query='aiter MLA')\n{SEPARATOR}")
        print("MCP 等价: search_signals(query='aiter MLA')")
        print()

        results = search.search(query="aiter MLA", limit=5)
        print(f"命中: {results['total']} 条  耗时: {results['meta']['query_time_ms']}ms")
        for sig in results["results"][:5]:
            _print_signal_row(sig)
            print()

        if results["total"] == 0:
            print("  (无结果 — DB 中可能没有匹配 'aiter MLA' 的 signal)")
            print()

        # ── 场景 2: 按 repo + state + labels 过滤 ─────────
        print(f"{SEPARATOR}\n场景 2: 按 repo + state + labels 过滤\n{SEPARATOR}")
        print("MCP 等价: execute_sql('SELECT ... WHERE source_repo=? AND github_state=? ...')")
        print()

        filtered = search.search(
            repos=["vllm-project/vllm"],
            state="open",
            labels=["rocm"],
            sort="updated",
            limit=5,
        )
        print(f"vllm/open/rocm: {filtered['total']} 条")
        for sig in filtered["results"][:3]:
            _print_signal_row(sig)
            print()

        if filtered["total"] == 0:
            print("  (无结果 — 尝试放宽过滤条件)")
            print()

        # ── 场景 3: 单条详情 + comments ───────────────────
        print(f"{SEPARATOR}\n场景 3: 查单条详情 + comments\n{SEPARATOR}")
        print("MCP 等价: get_signal_detail(signal_id)")
        print()

        sample_id = None
        if results["results"]:
            sample_id = results["results"][0]["signal_id"]
        elif filtered["results"]:
            sample_id = filtered["results"][0]["signal_id"]

        if sample_id is None:
            all_signals = search.search(limit=1)
            if all_signals["results"]:
                sample_id = all_signals["results"][0]["signal_id"]

        if sample_id:
            detail = search.get_detail(
                sample_id,
                include_comments=True,
                max_comments=10,
            )
            if detail:
                body = detail.get("body") or ""
                comments = detail.get("comments", [])
                cls = detail.get("classification_json")
                print(f"signal_id: {sample_id}")
                print(f"  title:     {detail['title']}")
                print(f"  body 长度: {len(body)} 字符")
                print(f"  body 前 200:")
                print(f"    {body[:200]}")
                print(f"  comments:  {len(comments)} 条")
                print(f"  分类状态:  {'已分类' if cls else '未分类'}")
                if cls:
                    print(f"  分类详情:  {json.dumps(cls, ensure_ascii=False)[:200]}")
                print()
            else:
                print(f"  signal {sample_id} 未找到 (get_detail 返回 None)")
                print()
        else:
            print("  ⚠️ DB 为空，跳过详情查询")
            print()

        # ── 场景 4: 变更历史 ─────────────────────────────
        print(f"{SEPARATOR}\n场景 4: 查变更历史\n{SEPARATOR}")
        print("MCP 等价: get_signal_changes(signal_id)")
        print()

        if sample_id:
            changes = repo.get_changes(sample_id, meaningful_only=True, limit=10)
            print(f"signal_id: {sample_id}")
            print(f"  有意义变更: {len(changes)} 条")
            for ch in changes[:5]:
                print(f"  - [{ch['change_type']}] {ch['changed_at']}")
                old_v = (ch.get("old_value") or "")[:50]
                new_v = (ch.get("new_value") or "")[:50]
                if old_v or new_v:
                    print(f"    old: {old_v}")
                    print(f"    new: {new_v}")
            if not changes:
                print("  (无变更记录 — 可能是首次同步)")
            print()
        else:
            print("  ⚠️ 无可用 signal，跳过")
            print()

        # ── 场景 5: gap 关联查询 ─────────────────────────
        print(f"{SEPARATOR}\n场景 5: 查 gap 关联的所有 signal\n{SEPARATOR}")
        print("MCP 等价: get_gap_signals(gap_id='gap_001')")
        print()

        gap_signals = search.search(gap_ids=["gap_001"], limit=10)
        print(f"gap_001 关联: {gap_signals['total']} 条")
        for sig in gap_signals["results"][:3]:
            _print_signal_row(sig)
            print()

        if gap_signals["total"] == 0:
            print("  (无关联 — gap_001 可能还没有被分类器标记过)")
            print("  提示: Module 2 分类后 gap_ids 才有数据")
            print()

        # ── 场景 6: 聚合统计 ─────────────────────────────
        print(f"{SEPARATOR}\n场景 6: 聚合统计\n{SEPARATOR}")
        print("MCP 等价: execute_sql('SELECT COUNT(*) ...')")
        print()

        total_signals = repo.count_signals()
        open_signals = repo.count_signals(state="open")
        closed_signals = repo.count_signals(state="closed")
        vllm_signals = repo.count_signals(source_repo="vllm-project/vllm")
        total_changes = repo.count_changes(meaningful_only=True)
        recent_changes = repo.count_changes(
            since="2026-04-01T00:00:00Z",
            meaningful_only=True,
        )

        print(f"  signal 总数:        {total_signals}")
        print(f"    open:             {open_signals}")
        print(f"    closed:           {closed_signals}")
        print(f"    vllm repo:        {vllm_signals}")
        print(f"  变更记录 (meaningful):")
        print(f"    总数:             {total_changes}")
        print(f"    2026-04 以来:     {recent_changes}")
        print()

    # ── 完成 ─────────────────────────────────────────────
    print(SEPARATOR)
    print("完成！以上 6 个场景覆盖了 Agent 最常用的查询模式。")
    print()
    print("Python 接口 vs MCP 对照:")
    print("  search.search(query=...)           → search_signals(query)")
    print("  search.get_detail(signal_id)       → get_signal_detail(signal_id)")
    print("  repo.get_changes(signal_id)        → get_signal_changes(signal_id)")
    print("  search.search(gap_ids=[...])       → get_gap_signals(gap_id)")
    print("  repo.count_signals() / count_changes() → execute_sql(sql)")
    print(SEPARATOR)


if __name__ == "__main__":
    main()
