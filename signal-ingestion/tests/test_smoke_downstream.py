"""Downstream smoke tests (SM-01 ~ SM-11).

Business-level tests driven by the pre-generated fixture DB.
No network, no mocks — real calls to SignalRepository / SignalSearch
asserting the contracts that Module 2 (Vivi) and Module 4 (Zijun) rely on.

Run:
    .venv/bin/python -m pytest tests/test_smoke_downstream.py -v
"""

import json
import shutil
from pathlib import Path

import pytest

from src.ingestion.change_detector import ChangeDetector, compute_content_hash
from src.ingestion.models import GitHubPayload, Signal
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    """Copy the pre-generated fixture DB to a temp location so tests can write without side effects."""
    src = Path(__file__).parent.parent / "data/fixtures/signals_fixture.db"
    if not src.exists():
        pytest.skip("fixture DB not generated — run scripts/generate_fixtures.py first")
    dst = tmp_path_factory.mktemp("smoke") / "signals.db"
    shutil.copy2(src, dst)
    return dst


# ═══════════════════════════════════════════════════════════════════════════
# SM-01 ~ SM-05: Vivi (Module 2) call patterns
# ═══════════════════════════════════════════════════════════════════════════


def test_sm01_vivi_full_pull(fixture_db):
    """Vivi 第一次拉取所有未分类 signal。"""
    with SignalRepository(fixture_db) as repo:
        feed = SignalSearch(repo).get_feed(
            since="1970-01-01T00:00:00Z", classified=False
        )
        assert len(feed["signals"]) >= 10
        for s in feed["signals"]:
            assert "signal_id" in s
            assert "title" in s
            assert "body_preview" in s
            assert "tags" in s and isinstance(s["tags"], list)
            assert "github_state" in s
            assert "github_labels" in s and isinstance(s["github_labels"], list)
            assert "recent_changes" in s and isinstance(s["recent_changes"], list)


def test_sm02_vivi_incremental_pull(fixture_db):
    """Vivi 用 since= 上次时间只看新/变的 signal。"""
    with SignalRepository(fixture_db) as repo:
        feed = SignalSearch(repo).get_feed(
            since="2099-01-01T00:00:00Z", classified=False
        )
        assert len(feed["signals"]) == 0


def test_sm03_vivi_get_detail(fixture_db):
    """get_detail 返回完整 body + comments。"""
    with SignalRepository(fixture_db) as repo:
        detail = SignalSearch(repo).get_detail(
            "github:vllm-project/vllm:issue:39303"
        )
        assert detail is not None
        assert len(detail["body"]) > 200
        assert "comments" in detail
        assert len(detail["comments"]) >= 5
        dates = [c["created_at"] for c in detail["comments"]]
        assert dates == sorted(dates)


def test_sm04_vivi_classify_and_writeback(fixture_db):
    """分类写回后，get_feed(classified=False) 不再返回这条。"""
    with SignalRepository(fixture_db) as repo:
        search = SignalSearch(repo)
        feed = search.get_feed(
            since="1970-01-01T00:00:00Z", classified=False
        )
        target = feed["signals"][0]
        sid = target["signal_id"]
        ok = repo.update_classification(
            sid,
            gap_ids=["gap_test"],
            signal_category="amd_fix",
            confidence=0.9,
            classifier_version="test_v1",
        )
        assert ok is True
        feed2 = search.get_feed(
            since="1970-01-01T00:00:00Z", classified=False
        )
        ids = [s["signal_id"] for s in feed2["signals"]]
        assert sid not in ids


def test_sm05_vivi_reclassify_decision(fixture_db):
    """已分类的 signal 如果有 recent_changes，Vivi 需要看到它。"""
    with SignalRepository(fixture_db) as repo:
        search = SignalSearch(repo)
        feed = search.get_feed(
            since="1970-01-01T00:00:00Z", classified=True
        )
        found = [s for s in feed["signals"] if s["source_number"] == 39303]
        if found:
            assert len(found[0]["recent_changes"]) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# SM-06 ~ SM-09: Zijun (Module 4) / Agent call patterns
# ═══════════════════════════════════════════════════════════════════════════


def test_sm06_agent_fts_search(fixture_db):
    """FTS5 搜索 'aiter MLA' 返回相关 signal。"""
    with SignalRepository(fixture_db) as repo:
        r = SignalSearch(repo).search(query="aiter MLA")
        assert r["total"] >= 1
        for s in r["results"]:
            assert "body_preview" in s
            assert "signal_id" in s


def test_sm07_agent_detail_with_comments(fixture_db):
    """Agent get_detail 拿到 comments + references。"""
    with SignalRepository(fixture_db) as repo:
        d = SignalSearch(repo).get_detail(
            "github:vllm-project/vllm:issue:39303",
            include_comments=True,
        )
        assert d is not None
        assert "comments" in d and isinstance(d["comments"], list)


def test_sm08_agent_get_changes(fixture_db):
    """get_changes 返回有意义变更。"""
    with SignalRepository(fixture_db) as repo:
        changes = repo.get_changes(
            signal_id="github:vllm-project/vllm:issue:39303",
            meaningful_only=True,
        )
        assert len(changes) >= 1
        for c in changes:
            assert "change_type" in c
            assert "changed_at" in c


def test_sm09_agent_gap_lookup(fixture_db):
    """#39303 已被分类到 gap_001，能按 gap_id 查到。"""
    with SignalRepository(fixture_db) as repo:
        search = SignalSearch(repo)
        r = search.search(gap_ids=["gap_001"])
        assert r["total"] >= 1
        ids = [s["signal_id"] for s in r["results"]]
        assert "github:vllm-project/vllm:issue:39303" in ids


# ═══════════════════════════════════════════════════════════════════════════
# SM-10 ~ SM-11: Cross-cutting
# ═══════════════════════════════════════════════════════════════════════════


def test_sm10_body_edit_overwrites_and_audits(fixture_db):
    """body 变化 → signals 表存最新，signal_changes 有 BODY_EDIT。"""
    with SignalRepository(fixture_db) as repo:
        old = repo.get_by_id("github:vllm-project/vllm:issue:39303")
        assert old is not None

        sig = Signal(
            signal_id=old["signal_id"],
            source_type=old["source_type"],
            source_url=old["source_url"],
            source_repo=old["source_repo"],
            source_number=old["source_number"],
            title=old["title"],
            body="COMPLETELY REWRITTEN BODY — this is totally different content " * 10,
            body_token_estimate=500,
            author=old["author"],
            created_at=old["created_at"],
            updated_at="2026-04-24T00:00:00Z",
            first_seen_at=old["first_seen_at"],
            last_synced_at="2026-04-24T00:00:00Z",
            content_hash="will_be_recomputed",
            tags=json.loads(old["tags"]) if old["tags"] else [],
            github=(
                GitHubPayload(**json.loads(old["github_json"]))
                if old["github_json"]
                else None
            ),
        )
        sig = sig.model_copy(update={"content_hash": compute_content_hash(sig)})

        det = ChangeDetector()
        events = det.detect(sig, existing_row=old, sync_run_id="test_edit")
        body_edits = [e for e in events if e.change_type == "body_edit"]
        assert len(body_edits) == 1
        assert body_edits[0].is_meaningful is True

        with repo.transaction():
            repo.upsert_signal(sig)
            repo.append_changes(events)

        updated = repo.get_by_id(sig.signal_id)
        assert "COMPLETELY REWRITTEN" in updated["body"]

        all_changes = repo.get_changes(
            signal_id=sig.signal_id, meaningful_only=False
        )
        assert any(c["change_type"] == "body_edit" for c in all_changes)


def test_sm11_cache_db_consistency(fixture_db):
    """cache JSON 的 signal_id 和 DB 匹配。"""
    cache_dir = Path(__file__).parent.parent / "data/fixtures/cache"
    if not cache_dir.exists():
        pytest.skip("fixture cache not generated")
    with SignalRepository(fixture_db) as repo:
        for p in cache_dir.rglob("*.json"):
            data = json.loads(p.read_text())
            sid = data.get("signal_id")
            if not sid:
                continue
            db_row = repo.get_by_id(sid)
            assert db_row is not None, f"cache has {sid} but DB doesn't"
            assert db_row["title"] == data["title"]


# ═══════════════════════════════════════════════════════════════════════════
# Vivi (Module 2) complete workflow
# ═══════════════════════════════════════════════════════════════════════════


class TestViviFullWorkflow:
    """Module 2 (Signal Classifier) complete workflow via Python API."""

    def test_vivi_feed_classify_verify(self, fixture_db):
        """Vivi's full loop: get_feed -> classify -> feed no longer returns it."""
        with SignalRepository(fixture_db) as repo:
            search = SignalSearch(repo)
            # 1. Get unclassified feed
            feed = search.get_feed(since="2000-01-01", classified=False, limit=10)
            assert feed["pagination"]["total"] > 0
            first_id = feed["signals"][0]["signal_id"]

            # 2. Classify it
            repo.update_classification(
                first_id,
                gap_ids=["gap_test_001"],
                signal_category="amd_gap",
                confidence=0.95,
                classifier_version="test_v1",
            )

            # 3. Verify: same feed query no longer returns this signal
            feed2 = search.get_feed(since="2000-01-01", classified=False, limit=10)
            ids_after = [s["signal_id"] for s in feed2["signals"]]
            assert first_id not in ids_after

            # 4. Verify: classified=True feed DOES contain it
            feed3 = search.get_feed(since="2000-01-01", classified=True, limit=10)
            ids_classified = [s["signal_id"] for s in feed3["signals"]]
            assert first_id in ids_classified


# ═══════════════════════════════════════════════════════════════════════════
# Agent (Module 4) query workflow
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentQueryWorkflow:
    """Module 4 (Agent) typical query patterns via Python API."""

    def test_search_then_detail_then_changes(self, fixture_db):
        """Agent flow: search -> pick first -> get_detail -> get_changes."""
        with SignalRepository(fixture_db) as repo:
            search = SignalSearch(repo)
            # 1. Search
            results = search.search(query="aiter MLA", limit=5)
            # 2. If results, get detail
            if results["total"] > 0:
                sid = results["results"][0]["signal_id"]
                detail = search.get_detail(sid, include_comments=True)
                assert detail is not None
                assert "title" in detail
                # 3. Get changes
                changes = repo.get_changes(signal_id=sid)
                assert isinstance(changes, list)

    def test_filter_by_repo_and_state(self, fixture_db):
        """Agent narrows down by repo + state."""
        with SignalRepository(fixture_db) as repo:
            search = SignalSearch(repo)
            results = search.search(
                repos=["vllm-project/vllm"], state="open", limit=10
            )
            for sig in results["results"]:
                assert (
                    sig.get("source_repo") == "vllm-project/vllm"
                    or "vllm" in sig.get("source_repo", "")
                )
