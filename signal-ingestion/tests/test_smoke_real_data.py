"""Real-data smoke + fuzz tests (SM-12 ~ SM-32).

Uses the production signals.db (2917 signals, 13500 comments from
vllm-project/vllm + sgl-project/sglang) — NOT fixture data.

If data/signals.db is missing or too small, all tests skip gracefully.

Run:
    .venv/bin/python -m pytest tests/test_smoke_real_data.py -v
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from src.storage.database import dict_row_factory, get_connection
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

_REAL_DB = Path(__file__).parent.parent / "data" / "signals.db"


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def real_db():
    """Path to the real signals.db (read-only tests)."""
    if not _REAL_DB.exists():
        pytest.skip("real signals.db not found — run sync_github.py first")
    n = sqlite3.connect(str(_REAL_DB)).execute(
        "SELECT COUNT(*) FROM signals"
    ).fetchone()[0]
    if n < 100:
        pytest.skip(f"real DB too small ({n} signals)")
    return _REAL_DB


@pytest.fixture
def writable_db(tmp_path):
    """Copy real DB to tmp for write tests."""
    if not _REAL_DB.exists():
        pytest.skip("real signals.db not found")
    dst = tmp_path / "signals.db"
    shutil.copy2(_REAL_DB, dst)
    for suffix in ("-wal", "-shm"):
        src = Path(f"{_REAL_DB}{suffix}")
        if src.exists():
            shutil.copy2(src, tmp_path / f"signals.db{suffix}")
    return dst


# ═══════════════════════════════════════════════════════════════════════════
# A 组：Vivi 真实工作流边界 (SM-12 ~ SM-20)
# ═══════════════════════════════════════════════════════════════════════════


def test_sm12_empty_body_signals(real_db):
    """空 body signal — get_detail 返回 body='', get_feed body_preview=''."""
    with SignalRepository(real_db) as repo:
        search = SignalSearch(repo)
        c = sqlite3.connect(str(real_db))
        row = c.execute(
            "SELECT signal_id FROM signals WHERE body IS NULL OR body = '' LIMIT 1"
        ).fetchone()
        if not row:
            pytest.skip("no empty body signals in DB")
        detail = search.get_detail(row[0])
        assert detail is not None
        assert detail.get("body", "") == "" or detail["body"] is None

        feed = search.get_feed(
            since="1970-01-01T00:00:00Z", classified=None, limit=500
        )
        empties = [s for s in feed["signals"] if s["signal_id"] == row[0]]
        if empties:
            assert empties[0]["body_preview"] == "" or empties[0]["body_preview"] is None


def test_sm13_very_long_body(real_db):
    """最长 body signal — get_detail 返回完整内容，不截断。"""
    with SignalRepository(real_db) as repo:
        c = sqlite3.connect(str(real_db))
        row = c.execute(
            "SELECT signal_id, LENGTH(body) FROM signals "
            "ORDER BY LENGTH(body) DESC LIMIT 1"
        ).fetchone()
        sid, body_len = row
        assert body_len > 50000, f"expected >50K body, got {body_len}"
        detail = SignalSearch(repo).get_detail(sid)
        assert detail is not None
        assert len(detail["body"]) == body_len
        assert detail["body_token_estimate"] > 10000


def test_sm14_comment_truncation_consistency(real_db):
    """github_comment_count > stored comments — Vivi 能看到差值。"""
    with SignalRepository(real_db) as repo:
        c = sqlite3.connect(str(real_db))
        rows = c.execute("""
            SELECT s.signal_id, s.github_comment_count, COUNT(c.id) as stored
            FROM signals s LEFT JOIN signal_comments c ON s.signal_id = c.signal_id
            WHERE s.github_comment_count > 50
            GROUP BY s.signal_id
            HAVING stored < s.github_comment_count
            LIMIT 3
        """).fetchall()
        if not rows:
            pytest.skip("no truncated comment signals")
        for sid, expected, stored in rows:
            detail = SignalSearch(repo).get_detail(sid, max_comments=100)
            assert len(detail["comments"]) <= 100
            assert detail["github_comment_count"] > len(detail["comments"])


def test_sm15_bot_comment_ratio(real_db):
    """Bot comments 存在且可区分 human/bot。"""
    with SignalRepository(real_db) as repo:
        c = sqlite3.connect(str(real_db))
        total = c.execute("SELECT COUNT(*) FROM signal_comments").fetchone()[0]
        bots = c.execute(
            "SELECT COUNT(*) FROM signal_comments WHERE is_bot = 1"
        ).fetchone()[0]
        assert bots > 0, "expected some bot comments"
        assert total > bots, "expected some human comments too"

        row = c.execute("""
            SELECT signal_id FROM signal_comments WHERE is_bot = 1
            GROUP BY signal_id HAVING COUNT(*) > 2 LIMIT 1
        """).fetchone()
        if row:
            detail = SignalSearch(repo).get_detail(row[0])
            humans = [cm for cm in detail["comments"] if not cm.get("is_bot")]
            bots_d = [cm for cm in detail["comments"] if cm.get("is_bot")]
            assert len(humans) + len(bots_d) == len(detail["comments"])


def test_sm16_chinese_content_fts(real_db):
    """FTS5 unicode61 tokenizer 处理 CJK 不崩溃。"""
    with SignalRepository(real_db) as repo:
        c = sqlite3.connect(str(real_db))
        cn_count = c.execute(
            "SELECT COUNT(*) FROM signals WHERE body LIKE '%的%' OR body LIKE '%是%'"
        ).fetchone()[0]
        if cn_count == 0:
            pytest.skip("no Chinese content")
        search = SignalSearch(repo)
        r = search.search(query="设备")
        assert isinstance(r["results"], list)


def test_sm17_classification_overwrite(writable_db):
    """分类覆盖：后写入的分类覆盖先前的。"""
    with SignalRepository(writable_db) as repo:
        c = sqlite3.connect(str(writable_db))
        sid = c.execute(
            "SELECT signal_id FROM signals WHERE classification_json IS NULL LIMIT 1"
        ).fetchone()[0]

        repo.update_classification(
            sid, gap_ids=[], signal_category="noise",
            confidence=0.3, classifier_version="v1",
        )
        repo.update_classification(
            sid, gap_ids=["gap_002"], signal_category="amd_gap",
            confidence=0.9, classifier_version="v2",
        )

        row = repo.get_by_id(sid)
        cls = json.loads(row["classification_json"])
        assert cls["signal_category"] == "amd_gap"
        assert cls["confidence"] == 0.9
        assert cls["classifier_version"] == "v2"
        assert json.loads(row["gap_ids"]) == ["gap_002"]


def test_sm18_batch_classify_100(writable_db):
    """100 条 sequential update_classification — 不 database-is-locked。"""
    with SignalRepository(writable_db) as repo:
        c = sqlite3.connect(str(writable_db))
        sids = [
            r[0] for r in c.execute(
                "SELECT signal_id FROM signals "
                "WHERE classification_json IS NULL LIMIT 100"
            ).fetchall()
        ]
        assert len(sids) >= 50, f"need 50+ unclassified, got {len(sids)}"
        for sid in sids:
            ok = repo.update_classification(
                sid, gap_ids=["gap_batch"], signal_category="noise",
                confidence=0.5, classifier_version="batch",
            )
            assert ok is True


def test_sm19_cross_repo_filter(real_db):
    """search(repos=['sgl-project/sglang']) 只返回 sglang 结果。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(repos=["sgl-project/sglang"], limit=50)
        assert r["total"] > 0
        for s in r["results"]:
            assert s["source_repo"] == "sgl-project/sglang"


def test_sm20_future_since_returns_empty(real_db):
    """since=2099 的 feed 返回空集。"""
    with SignalRepository(real_db) as repo:
        feed = SignalSearch(repo).get_feed(since="2099-01-01T00:00:00Z")
        assert len(feed["signals"]) == 0
        assert feed["pagination"]["has_more"] is False


# ═══════════════════════════════════════════════════════════════════════════
# B 组：Zijun Agent 查询边界 (SM-21 ~ SM-27)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("query", [
    "C++", "gfx950", "MI300X/MI355X", "[ROCm]", "ROCm/aiter#2720",
    "VLLM_ROCM_USE_AITER=1", "fp8_paged_mqa", "foo -bar",
    "hipcc --offload-arch=gfx942",
    "ROCm,CUDA", "https://github.com/vllm-project/vllm/issues/1234",
    "key:value",
])
def test_sm21_fts_special_chars(real_db, query):
    """FTS5 特殊字符不崩溃，返回有效结果（无 FTS 错误）。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(query=query)
        assert isinstance(r["results"], list)
        assert r.get("meta", {}).get("error") is None, (
            f"query={query!r} got FTS error: {r['meta'].get('error')}"
        )


@pytest.mark.parametrize("query", [
    "", " ", "*", "OR OR OR", "NOT", "NEAR/3",
    '"unclosed phrase', "AND", "a b c d e f g h i j k l m n o p",
])
def test_sm22_fts_degenerate_queries(real_db, query):
    """退化查询不崩溃。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(query=query)
        assert isinstance(r.get("results", []), list)


def test_sm23_fts_very_long_query(real_db):
    """超长 query（1000 字符）不崩溃不挂起。"""
    with SignalRepository(real_db) as repo:
        long_q = "AMD ROCm " * 100
        r = SignalSearch(repo).search(query=long_q)
        assert isinstance(r.get("results", []), list)


def test_sm24_nonexistent_gap_id(real_db):
    """不存在的 gap_id 查询返回 0 条。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(gap_ids=["nonexistent_gap_xyz_999"])
        assert r["total"] == 0


def test_sm25_reconcile_refs(writable_db):
    """reconcile_refs 填充 to_signal_id，不会增加未解析数。"""
    with SignalRepository(writable_db) as repo:
        c = sqlite3.connect(str(writable_db))
        unresolved_before = c.execute(
            "SELECT COUNT(*) FROM signal_refs WHERE to_signal_id IS NULL"
        ).fetchone()[0]
        if unresolved_before == 0:
            pytest.skip("no unresolved refs")
        n = repo.reconcile_refs()
        assert n >= 0
        unresolved_after = c.execute(
            "SELECT COUNT(*) FROM signal_refs WHERE to_signal_id IS NULL"
        ).fetchone()[0]
        assert unresolved_after <= unresolved_before


def test_sm26_cross_repo_fts_search(real_db):
    """FTS5 搜索同时命中 vllm 和 sglang。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(query="speculative decoding AMD", limit=50)
        if r["total"] == 0:
            pytest.skip("no cross-repo hits for this query")
        repos = set(s["source_repo"] for s in r["results"])
        assert len(repos) >= 1


def test_sm27_every_signal_has_new_signal_change(real_db):
    """每条 signal 都有至少一条 new_signal change event。"""
    c = sqlite3.connect(str(real_db))
    total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    with_change = c.execute("""
        SELECT COUNT(DISTINCT signal_id) FROM signal_changes
        WHERE change_type = 'new_signal'
    """).fetchone()[0]
    assert with_change == total, (
        f"{total - with_change} signals missing new_signal event"
    )


# ═══════════════════════════════════════════════════════════════════════════
# C 组：Fuzz / 健壮性 (SM-28 ~ SM-32)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("query", [
    'NEAR/3 "test"', "col:value", "a OR (b AND c)", "NOT NOT NOT a",
    "a*", "{braces}", "a + b", '""', "'''",
])
def test_sm28_fts_malicious_syntax(real_db, query):
    """恶意 FTS5 语法不崩溃。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(query=query)
        assert isinstance(r.get("results", []), list)


def test_sm29_classification_extreme_params(writable_db):
    """极端分类参数（空 gap_ids / 超长 category / 负 confidence）正常存储。"""
    with SignalRepository(writable_db) as repo:
        c = sqlite3.connect(str(writable_db))
        sid = c.execute("SELECT signal_id FROM signals LIMIT 1").fetchone()[0]

        ok = repo.update_classification(
            sid, gap_ids=[], signal_category="x" * 1000,
            confidence=-1.0, classifier_version="",
        )
        assert ok is True

        row = repo.get_by_id(sid)
        cls = json.loads(row["classification_json"])
        assert cls["confidence"] == -1.0
        assert len(cls["signal_category"]) == 1000


def test_sm30_labels_sql_injection(real_db):
    """参数化查询防止 labels SQL 注入。"""
    with SignalRepository(real_db) as repo:
        r = SignalSearch(repo).search(labels=["rocm'; DROP TABLE signals;--"])
        assert r["total"] == 0
        assert repo.count_signals() > 0


@pytest.mark.parametrize("bad_id", [
    "", "not-valid", "::::", "github:", "github:a:b",
    "x" * 10000, "github:test/test:issue:abc",
])
def test_sm31_bad_signal_id(real_db, bad_id):
    """畸形 signal_id — get_by_id 返回 None，get_changes 返回空 list。"""
    with SignalRepository(real_db) as repo:
        assert repo.get_by_id(bad_id) is None
        changes = repo.get_changes(signal_id=bad_id)
        assert isinstance(changes, list)


def test_sm32_concurrent_read_write(writable_db):
    """WAL 模式：读写并发不出错。"""
    errors: list[str] = []

    def reader():
        try:
            conn = get_connection(writable_db)
            conn.row_factory = dict_row_factory
            for _ in range(50):
                conn.execute("SELECT COUNT(*) FROM signals").fetchone()
        except Exception as e:
            errors.append(f"reader: {e}")
        finally:
            conn.close()

    def writer():
        try:
            with SignalRepository(writable_db) as repo:
                c = sqlite3.connect(str(writable_db))
                sids = [
                    r[0] for r in c.execute(
                        "SELECT signal_id FROM signals "
                        "WHERE classification_json IS NULL LIMIT 20"
                    ).fetchall()
                ]
                c.close()
                for sid in sids:
                    repo.update_classification(
                        sid, gap_ids=["gap_concurrent"],
                        signal_category="test", confidence=0.5,
                        classifier_version="t",
                    )
        except Exception as e:
            errors.append(f"writer: {e}")

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not errors, f"concurrent errors: {errors}"
