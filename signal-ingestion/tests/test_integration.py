"""Integration tests: full sync pipeline with MockAdapter + optional real GitHub.

Promotes the manual E2E scripts (``scripts/e2e_acceptance.py``,
``scripts/e2e_real_github.py``) into
proper pytest cases.

Group 1 — **Mock Integration** (no network, CI-safe):
  Module-scoped fixture runs 3 sync rounds once, individual tests assert
  against the shared DB / cache state.

Group 2 — **Real GitHub** (``@pytest.mark.network``):
  Smoke test that verifies GitHub API connectivity with minimal quota.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.ingestion.change_detector import ChangeDetector
from src.ingestion.models import (
    ChangeEvent,
    ChangeType,
    GitHubPayload,
    RawComment,
    RawSignal,
    Signal,
    SourceType,
    SyncMode,
)
from src.ingestion.normalizer import Normalizer
from src.ingestion.rate_limiter import GitHubRateLimiter
from src.storage.cache import JSONCache
from src.storage.database import dict_row_factory, get_connection, init_db
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch
from src.sync.orchestrator import SyncOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# MockAdapter — replays tests/fixtures/signals/*.json (from e2e_acceptance.py)
# ============================================================================


def _demo_issue_to_raw(d: dict, *, repo: str) -> dict:
    return {
        "number": d["number"],
        "title": d["title"],
        "state": d["state"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "html_url": d["html_url"],
        "user": {"login": d["author"]},
        "labels": [{"name": n} for n in d.get("labels", [])],
        "body": d.get("body_text", ""),
        "comments": d.get("comment_count", 0),
        "closed_at": d.get("closed_at"),
        "closed_by": {"login": d["closed_by"]} if d.get("closed_by") else None,
        "repository_url": f"https://api.github.com/repos/{repo}",
        "assignees": [],
    }


def _demo_pr_to_raw(d: dict, *, repo: str) -> dict:
    return {
        "number": d["number"],
        "title": d["title"],
        "state": d["state"],
        "created_at": d["created_at"],
        "updated_at": d.get("merged_at") or d["created_at"],
        "html_url": d["html_url"],
        "user": {"login": d["author"]},
        "labels": [{"name": n} for n in d.get("labels", [])],
        "body": d.get("body_text", ""),
        "comments": len(d.get("review_comments", [])),
        "closed_at": d.get("merged_at"),
        "closed_by": None,
        "repository_url": f"https://api.github.com/repos/{repo}",
        "assignees": [],
        "pull_request": {"url": d["html_url"]},
        "merged": d.get("merged", False),
        "merged_at": d.get("merged_at"),
        "merged_by": None,
        "changed_files": d.get("changed_files", {}).get("total", 0),
        "additions": d.get("changed_files", {}).get("additions", 0),
        "deletions": d.get("changed_files", {}).get("deletions", 0),
    }


def _discovery_item_to_raw(d: dict, *, repo: str) -> dict:
    is_pr = "/pull/" in d.get("html_url", "")
    raw: dict[str, Any] = {
        "number": d["number"],
        "title": d["title"],
        "state": d["state"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "html_url": d["html_url"],
        "user": {"login": d["author"]},
        "labels": [{"name": n} for n in d.get("labels", [])],
        "body": f"(summary only) {d['title']}",
        "comments": d.get("comment_count", 0),
        "closed_at": None,
        "closed_by": None,
        "repository_url": f"https://api.github.com/repos/{repo}",
        "assignees": [],
    }
    if is_pr:
        raw["pull_request"] = {"url": d["html_url"]}
    return raw


class MockAdapter:
    """Replays tests/fixtures/signals/*.json as adapter outputs."""

    source_type = SourceType.GITHUB_ISSUE

    def __init__(
        self,
        repo: str = "vllm-project/vllm",
        extra_comments_for_39303: int = 0,
    ):
        self.repo = repo
        self.rate_limiter = GitHubRateLimiter()
        self._extra = extra_comments_for_39303
        self._load_demos()

    def _load_demos(self) -> None:
        base = REPO_ROOT / "tests/fixtures/signals"
        self.issue_data = json.loads((base / "issue_39303.json").read_text())
        self.pr_data = json.loads((base / "pr_39616.json").read_text())
        discovery = json.loads((base / "discovery_vllm_rocm.json").read_text())
        self.discovery_items: list[dict] = discovery["signals"]

    def _raw_for_number(self, number: int) -> dict | None:
        if number == self.issue_data["number"]:
            raw = _demo_issue_to_raw(self.issue_data, repo=self.repo)
            raw["comments"] = raw["comments"] + self._extra
            if self._extra:
                raw["updated_at"] = "2026-04-22T00:00:00Z"
            return raw
        if number == self.pr_data["number"]:
            return _demo_pr_to_raw(self.pr_data, repo=self.repo)
        for item in self.discovery_items:
            if item["number"] == number:
                return _discovery_item_to_raw(item, repo=self.repo)
        return None

    async def discover(self, config, *, since=None):
        issue_raw = _demo_issue_to_raw(self.issue_data, repo=self.repo)
        issue_raw["comments"] = issue_raw["comments"] + self._extra
        if self._extra:
            issue_raw["updated_at"] = "2026-04-22T00:00:00Z"
        yield RawSignal(
            raw_id=str(self.issue_data["number"]),
            source_type=SourceType.GITHUB_ISSUE,
            raw_data=issue_raw,
        )
        yield RawSignal(
            raw_id=str(self.pr_data["number"]),
            source_type=SourceType.GITHUB_PR,
            raw_data=_demo_pr_to_raw(self.pr_data, repo=self.repo),
        )
        for item in self.discovery_items:
            if item["number"] in (
                self.issue_data["number"],
                self.pr_data["number"],
            ):
                continue
            is_pr = "/pull/" in item.get("html_url", "")
            yield RawSignal(
                raw_id=str(item["number"]),
                source_type=SourceType.GITHUB_PR if is_pr else SourceType.GITHUB_ISSUE,
                raw_data=_discovery_item_to_raw(item, repo=self.repo),
            )

    async def fetch_detail(self, raw_id, *, repo, **kw):
        n = int(raw_id)
        raw = self._raw_for_number(n)
        if raw is None:
            return None
        is_pr = "pull_request" in raw
        return RawSignal(
            raw_id=raw_id,
            source_type=SourceType.GITHUB_PR if is_pr else SourceType.GITHUB_ISSUE,
            raw_data=raw,
        )

    async def fetch_comments(self, raw_id, *, repo, since=None, max_comments=100):
        if int(raw_id) != self.issue_data["number"]:
            return []
        extras = [
            "New follow-up: regression also hits gfx1100.",
            "PR #40000 landed, please retest on nightly.",
            "Confirmed fixed on nightly 2026-04-22.",
        ][: self._extra]
        bodies = [
            "Thanks for the thorough investigation. Looking into it.",
            "I confirmed the issue on our MI355X. Will bisect aiter commits.",
            "It looks like a regression introduced in #39200 aiter update.",
            "CI passed",
            "Signed-off-by: bot@amd.com",
            "We've identified the kernel — fix at ROCm/aiter#2720.",
            "This comment was marked as resolved.",
            "Merged fix in PR #39616. Closing.",
            "Reopening — fix didn't cover the gfx942 path. cc @hongxiayang",
            "Second fix landed. Please re-test.",
            "Confirmed fixed in aiter v0.9.3. Thanks!",
        ] + extras
        out = []
        for i, body in enumerate(bodies[:max_comments], start=1):
            out.append(
                RawComment(
                    comment_id=f"c{i}",
                    author="github-actions[bot]" if i in (4, 7) else "dev_user",
                    body=body,
                    created_at=f"2026-04-{10 + i:02d}T00:00:00Z",
                    raw_data={
                        "id": i,
                        "body": body,
                        "created_at": f"2026-04-{10 + i:02d}T00:00:00Z",
                    },
                )
            )
        return out

    def make_signal_id(self, raw):
        kind = "pr" if raw.get("pull_request") else "issue"
        return f"github:{self.repo}:{kind}:{raw['number']}"


# ============================================================================
# Module-scoped fixture: run 3 sync rounds once, share results across tests
# ============================================================================


class SyncState:
    """Captures the DB / cache / run results after the 3-round sync."""

    run1: Any
    run2: Any
    run3: Any
    db_path: Path
    cache_dir: Path
    conn: Any
    repo: SignalRepository
    search: SignalSearch


@pytest.fixture(scope="module")
def sync_state(tmp_path_factory) -> SyncState:
    """Execute the 3-round mock sync pipeline once for the entire module."""
    base = tmp_path_factory.mktemp("integration")
    db_path = base / "signals.db"
    cache_dir = base / "cache"
    init_db(db_path)

    conn = get_connection(db_path)
    conn.row_factory = dict_row_factory
    repo = SignalRepository(connection=conn)

    def _make_orch(adapter):
        return SyncOrchestrator(
            adapter=adapter,
            normalizer=Normalizer(),
            change_detector=ChangeDetector(),
            repository=repo,
            cache=JSONCache(cache_dir),
        )

    async def _run_all():
        # Run 1: first full sync — 22 new signals + comments for #39303
        orch1 = _make_orch(MockAdapter())
        r1 = await orch1.run(
            source_repo="vllm-project/vllm",
            sync_mode=SyncMode.FULL,
            labels=["rocm"],
            include_comments=True,
            max_comments_per_issue=50,
        )

        # Run 2: identical content — everything unchanged
        orch2 = _make_orch(MockAdapter())
        r2 = await orch2.run(
            source_repo="vllm-project/vllm",
            sync_mode=SyncMode.FULL,
            labels=["rocm"],
            include_comments=True,
        )

        # Run 3: #39303 gained 3 new comments → triggers NEW_COMMENT
        orch3 = _make_orch(MockAdapter(extra_comments_for_39303=3))
        r3 = await orch3.run(
            source_repo="vllm-project/vllm",
            sync_mode=SyncMode.TARGETED,
            target_numbers=[39303],
            include_comments=True,
        )
        return r1, r2, r3

    r1, r2, r3 = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _run_all()
    )

    state = SyncState()
    state.run1 = r1
    state.run2 = r2
    state.run3 = r3
    state.db_path = db_path
    state.cache_dir = cache_dir
    state.conn = conn
    state.repo = repo
    state.search = SignalSearch(repo)
    return state


# ============================================================================
# Group 1: Mock Integration — 3-round sync pipeline
# ============================================================================


class TestSyncRun1FirstFull:
    """Run 1: first full sync should ingest 22 new signals + 11 comments."""

    def test_run1_completed(self, sync_state: SyncState):
        status = sync_state.run1.status
        assert (status.value if hasattr(status, "value") else status) == "completed"

    def test_run1_total_signals(self, sync_state: SyncState):
        assert sync_state.run1.signals_total == 22

    def test_run1_all_new(self, sync_state: SyncState):
        assert sync_state.run1.signals_created == 22

    def test_run1_comments_fetched(self, sync_state: SyncState):
        assert sync_state.run1.comments_fetched == 11


class TestSyncRun2Idempotent:
    """Run 2: identical re-sync should detect all 22 as unchanged."""

    def test_run2_completed(self, sync_state: SyncState):
        status = sync_state.run2.status
        assert (status.value if hasattr(status, "value") else status) == "completed"

    def test_run2_all_unchanged(self, sync_state: SyncState):
        assert sync_state.run2.signals_unchanged == 22

    def test_run2_no_new(self, sync_state: SyncState):
        assert sync_state.run2.signals_created == 0

    def test_run2_no_comments(self, sync_state: SyncState):
        assert sync_state.run2.comments_fetched == 0


class TestSyncRun3CommentChange:
    """Run 3: comment_count increase triggers update + NEW_COMMENT event."""

    def test_run3_completed(self, sync_state: SyncState):
        status = sync_state.run3.status
        assert (status.value if hasattr(status, "value") else status) == "completed"

    def test_run3_updated_count(self, sync_state: SyncState):
        assert sync_state.run3.signals_updated >= 1

    def test_run3_comments_fetched(self, sync_state: SyncState):
        assert sync_state.run3.comments_fetched >= 11


# ============================================================================
# Group 1: MVP acceptance criteria
# ============================================================================


class TestAcceptanceCriteria:
    """6 MVP acceptance criteria verified against the shared sync_state."""

    # [1] signals count >= 22
    def test_criterion1_signal_count(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        c.close()
        assert total >= 22

    # [2] all signals have 64-char content_hash + version >= 1
    def test_criterion2_content_hash_and_version(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        hash_ok = c.execute(
            "SELECT COUNT(*) FROM signals WHERE LENGTH(content_hash) = 64"
        ).fetchone()[0]
        bad_version = c.execute(
            "SELECT COUNT(*) FROM signals WHERE version < 1"
        ).fetchone()[0]
        c.close()
        assert hash_ok == total, f"Only {hash_ok}/{total} have 64-char hash"
        assert bad_version == 0, f"{bad_version} signal(s) with version < 1"

    # [3] FTS5 search "aiter MLA" has results
    def test_criterion3_fts5_search(self, sync_state: SyncState):
        result = sync_state.search.search(query="aiter MLA", limit=5)
        assert result["total"] >= 1

    # [4] change detection produced NEW_COMMENT event
    def test_criterion4_new_comment_event(self, sync_state: SyncState):
        changes = sync_state.repo.get_changes(meaningful_only=False, limit=500)
        nc = [ch for ch in changes if ch["change_type"] == "new_comment"]
        assert len(nc) >= 1, "Expected at least 1 NEW_COMMENT change event"

    # [5] signal_changes has audit records
    def test_criterion5_audit_trail(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        n = c.execute("SELECT COUNT(*) FROM signal_changes").fetchone()[0]
        c.close()
        assert n >= 1, "signal_changes table should have audit records"

    # [6] cache file exists
    def test_criterion6_cache_exists(self, sync_state: SyncState):
        issues_dir = sync_state.cache_dir / "github/vllm-project_vllm/issues"
        key_cache = issues_dir / "39303.json"
        assert key_cache.exists(), "39303.json cache file should exist"
        data = json.loads(key_cache.read_text())
        assert data["signal_id"] == "github:vllm-project/vllm:issue:39303"


# ============================================================================
# Group 1: Feed enhancement
# ============================================================================


class TestFeedEnhancement:
    """Verify D3.1 feed fields: body_preview, recent_changes, total_token_estimate."""

    def test_feed_returns_signals(self, sync_state: SyncState):
        feed = sync_state.search.get_feed(
            since="2026-01-01T00:00:00Z", classified=None
        )
        assert len(feed["signals"]) >= 1

    def test_body_preview_present_and_bounded(self, sync_state: SyncState):
        feed = sync_state.search.get_feed(
            since="2026-01-01T00:00:00Z", classified=None
        )
        for s in feed["signals"]:
            assert "body_preview" in s
            assert len(s["body_preview"]) <= 200

    def test_recent_changes_attached(self, sync_state: SyncState):
        feed = sync_state.search.get_feed(
            since="2026-01-01T00:00:00Z", classified=None
        )
        sig_39303 = next(
            (s for s in feed["signals"]
             if s.get("source_number") == 39303),
            None,
        )
        assert sig_39303 is not None, "#39303 should be in the feed"
        assert "recent_changes" in sig_39303
        rc = sig_39303["recent_changes"]
        assert len(rc) >= 1
        change_types = {c["change_type"] for c in rc}
        assert "new_comment" in change_types

    def test_total_token_estimate_in_meta(self, sync_state: SyncState):
        feed = sync_state.search.get_feed(
            since="2026-01-01T00:00:00Z", classified=None
        )
        meta = feed.get("meta", {})
        assert "total_token_estimate" in meta
        assert meta["total_token_estimate"] > 0


# ============================================================================
# Group 1: Additional DB integrity checks
# ============================================================================


class TestDBIntegrity:
    """Post-sync DB state sanity checks."""

    def test_sync_runs_recorded(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        n = c.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        c.close()
        assert n >= 3

    def test_comments_stored_for_39303(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        n = c.execute(
            "SELECT COUNT(*) FROM signal_comments "
            "WHERE signal_id = 'github:vllm-project/vllm:issue:39303'"
        ).fetchone()[0]
        c.close()
        assert n >= 11

    def test_signal_39303_has_correct_state(self, sync_state: SyncState):
        row = sync_state.repo.get_by_id("github:vllm-project/vllm:issue:39303")
        assert row is not None
        assert len(row["content_hash"]) == 64
        assert row["version"] >= 1

    def test_issue_and_pr_types_present(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        prs = c.execute(
            "SELECT COUNT(*) FROM signals WHERE github_is_pr = 1"
        ).fetchone()[0]
        issues = c.execute(
            "SELECT COUNT(*) FROM signals WHERE github_is_pr = 0"
        ).fetchone()[0]
        c.close()
        assert prs >= 1, "Should have at least 1 PR"
        assert issues >= 1, "Should have at least 1 issue"

    def test_distinct_content_hashes(self, sync_state: SyncState):
        c = sqlite3.connect(sync_state.db_path)
        distinct = c.execute(
            "SELECT COUNT(DISTINCT content_hash) FROM signals"
        ).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        c.close()
        assert distinct == total, "Each signal should have a unique content_hash"


# ============================================================================
# Group 2: Real GitHub (network-dependent, opt-in)
# ============================================================================


@pytest.mark.network
class TestRealGitHub:
    """Smoke tests that require actual GitHub API access."""

    @pytest.fixture(autouse=True)
    def _require_token(self):
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")

    def test_basic_connectivity(self, tmp_path):
        """Can we reach the GitHub API and ingest at least 1 signal?"""
        from src.ingestion.adapters.github_adapter import GitHubAdapter

        db_path = tmp_path / "real_gh.db"
        init_db(db_path)
        conn = get_connection(db_path)
        conn.row_factory = dict_row_factory
        repo = SignalRepository(connection=conn)

        token = os.environ["GITHUB_TOKEN"]
        adapter = GitHubAdapter(token=token)

        orch = SyncOrchestrator(
            adapter=adapter,
            normalizer=Normalizer(),
            change_detector=ChangeDetector(),
            repository=repo,
            cache=JSONCache(tmp_path / "cache"),
        )

        async def _run():
            return await orch.run(
                source_repo="vllm-project/vllm",
                sync_mode=SyncMode.FULL,
                labels=["rocm"],
                include_comments=False,
                max_pages=1,
            )

        result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _run()
        )
        repo.close()

        assert result.signals_total >= 1, "Should ingest at least 1 signal from GitHub"
        st = result.status
        status_str = st.value if hasattr(st, "value") else st
        assert status_str in ("completed", "partial")


# ============================================================================
# Group 3: build_default_orchestrator env var loading
# ============================================================================


class TestBuildOrchestratorEnv:
    """Tests for build_default_orchestrator env var loading."""

    @pytest.fixture(autouse=True)
    def _clean_github_env(self, monkeypatch):
        """Remove all GITHUB_* env vars so .env pollution doesn't leak in."""
        monkeypatch.delenv("GITHUB_TOKENS", raising=False)
        for idx in range(1, 21):
            for suffix in ("ID", "INSTALLATION_ID", "PEM_PATH"):
                monkeypatch.delenv(f"GITHUB_APP_{idx}_{suffix}", raising=False)

    def test_github_tokens_creates_pat_pool(self, tmp_path, monkeypatch):
        """GITHUB_TOKENS=a,b,c creates a 3-token PAT pool."""
        monkeypatch.setenv("GITHUB_TOKENS", "ghp_aaa,ghp_bbb,ghp_ccc")
        init_db(tmp_path / "test.db")
        from src.sync.orchestrator import build_default_orchestrator

        orch, repo = build_default_orchestrator(db_path=tmp_path / "test.db")
        assert orch.adapter._token_pool is not None
        assert orch.adapter._token_pool.pool_size == 3
        repo.close()

    def test_github_app_env_creates_app_entries(self, tmp_path, monkeypatch):
        """GITHUB_APP_1_* env vars create app entries in pool."""
        pem_path = tmp_path / "fake.pem"
        pem_path.write_text("fake-key")
        monkeypatch.setenv("GITHUB_APP_1_ID", "123")
        monkeypatch.setenv("GITHUB_APP_1_INSTALLATION_ID", "456")
        monkeypatch.setenv("GITHUB_APP_1_PEM_PATH", str(pem_path))
        init_db(tmp_path / "test.db")
        from src.sync.orchestrator import build_default_orchestrator

        orch, repo = build_default_orchestrator(db_path=tmp_path / "test.db")
        assert orch.adapter._token_pool is not None
        assert orch.adapter._token_pool.pool_size == 1
        repo.close()

    def test_mixed_pat_and_app(self, tmp_path, monkeypatch):
        """PATs + App entries coexist in the pool."""
        pem_path = tmp_path / "fake.pem"
        pem_path.write_text("fake-key")
        monkeypatch.setenv("GITHUB_TOKENS", "ghp_aaa")
        monkeypatch.setenv("GITHUB_APP_1_ID", "123")
        monkeypatch.setenv("GITHUB_APP_1_INSTALLATION_ID", "456")
        monkeypatch.setenv("GITHUB_APP_1_PEM_PATH", str(pem_path))
        init_db(tmp_path / "test.db")
        from src.sync.orchestrator import build_default_orchestrator

        orch, repo = build_default_orchestrator(db_path=tmp_path / "test.db")
        assert orch.adapter._token_pool is not None
        assert orch.adapter._token_pool.pool_size == 2
        repo.close()

    def test_missing_pem_skips_app(self, tmp_path, monkeypatch):
        """Missing PEM file logs warning and skips that app."""
        monkeypatch.setenv("GITHUB_APP_1_ID", "123")
        monkeypatch.setenv("GITHUB_APP_1_INSTALLATION_ID", "456")
        monkeypatch.setenv("GITHUB_APP_1_PEM_PATH", "/nonexistent/fake.pem")
        init_db(tmp_path / "test.db")
        from src.sync.orchestrator import build_default_orchestrator

        orch, repo = build_default_orchestrator(db_path=tmp_path / "test.db")
        assert orch.adapter._token_pool is None
        repo.close()
