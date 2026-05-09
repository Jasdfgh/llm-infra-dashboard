#!/usr/bin/env python3
"""Module 2 (Signal Classifier) quickstart example — for Vivi.

Usage:
    .venv/bin/python examples/module2_quickstart.py

This script demonstrates Vivi's complete workflow (7 steps):
  1. Connect to DB
  2. Fetch unclassified signals (get_feed)
  3. Preview the list
  4. Inspect the first signal (get_detail)
  5. Simulate classification (your_classifier — replace with your LLM logic)
  6. Write back classification (update_classification)
  7. Verify (re-fetch feed, confirm the signal is gone)

Interface version:
    from src import INTERFACE_VERSION  # pinned to "1.0"

Dependencies: Python stdlib + src/ only (no extra packages)

Daily workflow (run once per day):
  1. Ensure sync has completed (check sync_runs in data/signals.db)
  2. Run this script to fetch unclassified signals
  3. Replace your_classifier() with your LLM classifier
  4. Classification results are automatically written back to DB

Scheduling tips:
  Add sync_github.py to crontab (see examples/ops_cheatsheet.md)
  Then make this script the entry point of your classification pipeline
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

# ─────────────────────────────────────────────────────────────
# DB path: prefer fixture DB, fallback to signals.db
# ─────────────────────────────────────────────────────────────
_FIXTURE_DB = PROJECT_ROOT / "data" / "fixtures" / "signals_fixture.db"
_MAIN_DB = PROJECT_ROOT / "data" / "signals.db"

if _FIXTURE_DB.exists():
    DB_PATH = _FIXTURE_DB
elif _MAIN_DB.exists():
    DB_PATH = _MAIN_DB
    print("⚠️  WARNING: Using production database (signals.db).")
    print("   This example WILL WRITE classification results to it.")
    print("   Run `scripts/generate_fixtures.py` first to create a safe fixture DB.\n")
else:
    print("❌ 找不到数据库文件。请先运行:")
    print("   .venv/bin/python scripts/init_db.py")
    print("   .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full")
    print()
    print(f"   期望路径: {_FIXTURE_DB}")
    print(f"         或: {_MAIN_DB}")
    sys.exit(1)

SEPARATOR = "=" * 60


# ─────────────────────────────────────────────────────────────
# Step 5 placeholder: replace with your LLM classification logic
# ─────────────────────────────────────────────────────────────
def your_classifier(detail: dict) -> tuple[str, list[str], float]:
    """Placeholder classifier — returns (signal_category, gap_ids, confidence).

    *** Replace with your LLM classification logic ***
    For example: feed detail["title"] + detail["body"] to GPT/Claude
    and have it return classification labels and an associated gap_id list.
    """
    return "amd_gap", ["gap_001"], 0.85


def main() -> None:
    print(SEPARATOR)
    print("Module 2 Quickstart — Vivi Signal Classifier 接入示例")
    print(SEPARATOR)
    print(f"数据库: {DB_PATH}")
    print()

        # ── Step 1: Connect to DB ──────────────────────────────
    print(f"{SEPARATOR}\nStep 1: 连接数据库\n{SEPARATOR}")
    with SignalRepository(DB_PATH) as repo:
        search = SignalSearch(repo)
        print(f"✅ 已连接: {DB_PATH.name}")
        print()

        # ── Step 2: Fetch unclassified signals ─────────────
        print(f"{SEPARATOR}\nStep 2: 拉取未分类 signal (get_feed)\n{SEPARATOR}")
        feed = search.get_feed(
            since="2000-01-01T00:00:00Z",
            classified=False,
            limit=20,
        )

        signals = feed["signals"]
        pagination = feed["pagination"]
        print(f"总数: {pagination['total']}  本页: {pagination['returned']}  "
              f"has_more: {pagination['has_more']}")
        print()

        if not signals:
            print("⚠️  没有未分类的 signal。")
            print("   可能原因: 所有 signal 都已分类，或者 DB 为空。")
            print("   请先运行 sync_github.py 拉取数据。")
            return

        # ── Step 3: Preview the list ──────────────────────
        print(f"{SEPARATOR}\nStep 3: 预览 signal 列表\n{SEPARATOR}")
        for i, sig in enumerate(signals[:10], 1):
            changes = sig.get("recent_changes", [])
            change_summary = ", ".join(
                c.get("change_type", "?") for c in changes[:3]
            ) if changes else "(无变更记录)"

            print(f"  [{i}] {sig['signal_id']}")
            print(f"      title:   {sig['title'][:80]}")
            print(f"      preview: {sig['body_preview'][:100]}...")
            print(f"      changes: {change_summary}")
            print()

        if pagination["total"] > 10:
            print(f"  ... 还有 {pagination['total'] - 10} 条未显示")
            print()

        # ── Step 4: Inspect the first signal ──────────────
        print(f"{SEPARATOR}\nStep 4: 深入查看第一条 signal (get_detail)\n{SEPARATOR}")
        target = signals[0]
        target_id = target["signal_id"]
        print(f"signal_id: {target_id}")
        print()

        detail = search.get_detail(target_id, include_comments=True)
        if detail is None:
            print(f"⚠️  get_detail 返回 None (signal 可能已被删除)")
            return

        body = detail.get("body") or ""
        comments = detail.get("comments", [])
        print(f"  title:    {detail['title']}")
        print(f"  body 前 300 字符:")
        print(f"    {body[:300]}")
        print(f"  comments 数量: {len(comments)}")
        if comments:
            first_comment = comments[0]
            print(f"  第一条 comment ({first_comment.get('author', '?')}):")
            comment_body = first_comment.get("body", "")
            print(f"    {comment_body[:150]}...")
        print()

        # ── Step 5: Simulate classification ──────────────
        print(f"{SEPARATOR}\nStep 5: 模拟分类 (your_classifier)\n{SEPARATOR}")
        category, gap_ids, confidence = your_classifier(detail)
        print(f"  分类结果:")
        print(f"    category:   {category}")
        print(f"    gap_ids:    {gap_ids}")
        print(f"    confidence: {confidence}")
        print()

        # ── Step 6: Write back classification ─────────────
        print(f"{SEPARATOR}\nStep 6: 写回分类结果 (update_classification)\n{SEPARATOR}")
        ok = repo.update_classification(
            target_id,
            gap_ids=gap_ids,
            signal_category=category,
            confidence=confidence,
            classifier_version="example-v0.1",
        )
        if ok:
            print(f"✅ 写回成功: {target_id}")
        else:
            print(f"❌ 写回失败: signal 不存在 ({target_id})")
            return
        print()

        # ── Step 7: Verify ────────────────────────────────
        print(f"{SEPARATOR}\nStep 7: 验证 — 再拉 feed 确认该条不在了\n{SEPARATOR}")
        feed2 = search.get_feed(
            since="2000-01-01T00:00:00Z",
            classified=False,
            limit=20,
        )
        new_ids = {s["signal_id"] for s in feed2["signals"]}

        if target_id not in new_ids:
            print(f"✅ 验证通过: {target_id} 已不在未分类列表中")
        else:
            print(f"⚠️  {target_id} 仍在未分类列表中 (可能是事务尚未提交)")
        print(f"   未分类剩余: {feed2['pagination']['total']} 条")
        print()

    # ── Done ──────────────────────────────────────────────
    print(SEPARATOR)
    print("完成！你可以把 `your_classifier()` 替换成你的 LLM 分类逻辑。")
    print()
    print("典型做法:")
    print("  1. 把 detail['title'] + detail['body'] + detail['comments']")
    print("     拼成 prompt，送给你的 LLM")
    print("  2. 解析 LLM 输出得到 category / gap_ids / confidence")
    print("  3. 调用 repo.update_classification() 写回")
    print()
    print("接口版本控制:")
    print("  from src import INTERFACE_VERSION  # 当前 '1.0'")
    print(SEPARATOR)


if __name__ == "__main__":
    main()
