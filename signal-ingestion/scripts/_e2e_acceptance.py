"""End-to-end acceptance test using demo JSON + synthetic data.

Independent of the GitHub API so we can verify all 6 MVP acceptance
criteria without hitting rate limits. Uses the same orchestrator code
path as scripts/sync_github.py but injects a MockAdapter that replays
demo/signals/*.json as raw GitHub payloads.

Not intended for production — this is a self-test only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.ingestion.change_detector import ChangeDetector  # noqa: E402
from src.ingestion.models import (  # noqa: E402
    RawComment,
    RawSignal,
    SourceType,
    SyncMode,
)
from src.ingestion.normalizer import Normalizer  # noqa: E402
from src.ingestion.rate_limiter import GitHubRateLimiter  # noqa: E402
from src.storage.cache import JSONCache  # noqa: E402
from src.storage.database import (  # noqa: E402
    dict_row_factory,
    get_connection,
    init_db,
)
from src.storage.repository import SignalRepository  # noqa: E402
from src.storage.search import SignalSearch  # noqa: E402
from src.sync.orchestrator import SyncOrchestrator  # noqa: E402

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
)


# ---------------------------------------------------------------------------
# MockAdapter: replays demo JSON as if it came from GitHub API
# ---------------------------------------------------------------------------


def _demo_issue_to_raw(d: dict, *, repo: str) -> dict:
    """Turn demo/signals/issue_*.json into a raw GitHub API-shaped dict."""
    raw = {
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
    return raw


def _demo_pr_to_raw(d: dict, *, repo: str) -> dict:
    """Turn demo/signals/pr_*.json into a raw API-shaped dict."""
    raw = {
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
        # PR-specific
        "pull_request": {"url": d["html_url"]},
        "merged": d.get("merged", False),
        "merged_at": d.get("merged_at"),
        "merged_by": None,
        "changed_files": d.get("changed_files", {}).get("total", 0),
        "additions": d.get("changed_files", {}).get("additions", 0),
        "deletions": d.get("changed_files", {}).get("deletions", 0),
    }
    return raw


def _discovery_item_to_raw(d: dict, *, repo: str) -> dict:
    """Turn one item from demo/signals/discovery_*.json into raw shape."""
    is_pr = "/pull/" in d.get("html_url", "")
    raw = {
        "number": d["number"],
        "title": d["title"],
        "state": d["state"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "html_url": d["html_url"],
        "user": {"login": d["author"]},
        "labels": [{"name": n} for n in d.get("labels", [])],
        # Discovery file only has summary — use title as placeholder body
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
    """Replays demo/signals/*.json as adapter outputs.

    `extra_comments_for_39303`: simulate that N new comments appeared on
    issue #39303 since the last sync, so ChangeDetector emits NEW_COMMENT.
    """

    source_type = SourceType.GITHUB_ISSUE

    def __init__(self, repo: str = "vllm-project/vllm", extra_comments_for_39303: int = 0):
        self.repo = repo
        self.rate_limiter = GitHubRateLimiter()
        self._extra = extra_comments_for_39303
        self._load_demos()

    def _load_demos(self) -> None:
        base = _REPO_ROOT / "demo/signals"
        self.issue_data = json.loads((base / "issue_39303.json").read_text())
        self.pr_data = json.loads((base / "pr_39616.json").read_text())
        discovery = json.loads((base / "discovery_vllm_rocm.json").read_text())
        self.discovery_items: list[dict] = discovery["signals"]

    def _raw_for_number(self, number: int) -> dict | None:
        if number == self.issue_data["number"]:
            raw = _demo_issue_to_raw(self.issue_data, repo=self.repo)
            raw["comments"] = raw["comments"] + self._extra
            if self._extra:
                # A new comment is inherently an update to the issue
                raw["updated_at"] = "2026-04-22T00:00:00Z"
            return raw
        if number == self.pr_data["number"]:
            return _demo_pr_to_raw(self.pr_data, repo=self.repo)
        for item in self.discovery_items:
            if item["number"] == number:
                return _discovery_item_to_raw(item, repo=self.repo)
        return None

    async def discover(self, config, *, since=None):
        """Yield: issue_39303 + pr_39616 + 20 discovery items → 22 raw signals."""
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
        """Return comments only for #39303 (11 + `extra` simulated)."""
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
            "CI passed",  # auto-pattern → is_bot=False but filtered as noise
            "Signed-off-by: bot@amd.com",  # filtered as auto pattern
            "We've identified the kernel — fix at ROCm/aiter#2720.",
            "This comment was marked as resolved.",  # auto pattern
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


# ---------------------------------------------------------------------------
# Acceptance runner
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    print()
    print(f"=== {title} ===")


async def main() -> int:
    db_path = _REPO_ROOT / "data/signals.db"
    cache_dir = _REPO_ROOT / "data/cache"

    # Clean slate
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if p.exists():
            p.unlink()
    if cache_dir.exists():
        shutil.rmtree(cache_dir)

    _print_section("STEP 1: init_db")
    init_db(db_path)
    print(f"DB created at {db_path}")

    # Build orchestrator with MockAdapter
    conn = get_connection(db_path)
    conn.row_factory = dict_row_factory
    repo = SignalRepository(connection=conn)
    adapter = MockAdapter()
    orchestrator = SyncOrchestrator(
        adapter=adapter,
        normalizer=Normalizer(),
        change_detector=ChangeDetector(),
        repository=repo,
        cache=JSONCache(cache_dir),
    )

    _print_section("STEP 2: First full sync (22 signals + comments for #39303)")
    run1 = await orchestrator.run(
        source_repo="vllm-project/vllm",
        sync_mode=SyncMode.FULL,
        labels=["rocm"],
        include_comments=True,
        max_comments_per_issue=50,
    )
    print(
        f"Run 1 {run1.status}: total={run1.signals_total} "
        f"new={run1.signals_created} unchanged={run1.signals_unchanged} "
        f"comments={run1.comments_fetched}"
    )

    _print_section("STEP 3: Second sync — same content, should be all unchanged")
    # Create a fresh adapter (resets rate_limiter counter)
    orchestrator2 = SyncOrchestrator(
        adapter=MockAdapter(),
        normalizer=Normalizer(),
        change_detector=ChangeDetector(),
        repository=repo,
        cache=JSONCache(cache_dir),
    )
    run2 = await orchestrator2.run(
        source_repo="vllm-project/vllm",
        sync_mode=SyncMode.FULL,
        labels=["rocm"],
        include_comments=True,
    )
    print(
        f"Run 2 {run2.status}: total={run2.signals_total} "
        f"new={run2.signals_created} updated={run2.signals_updated} "
        f"unchanged={run2.signals_unchanged} comments={run2.comments_fetched}"
    )

    _print_section("STEP 4: Run 3 — #39303 got 3 new comments on GitHub")
    # MockAdapter with extra_comments_for_39303=3 simulates what would happen
    # if GitHub actually had 3 new comments posted since last sync.
    orchestrator3 = SyncOrchestrator(
        adapter=MockAdapter(extra_comments_for_39303=3),
        normalizer=Normalizer(),
        change_detector=ChangeDetector(),
        repository=repo,
        cache=JSONCache(cache_dir),
    )
    run3 = await orchestrator3.run(
        source_repo="vllm-project/vllm",
        sync_mode=SyncMode.TARGETED,
        target_numbers=[39303],
        include_comments=True,
    )
    print(
        f"Run 3 {run3.status}: total={run3.signals_total} "
        f"updated={run3.signals_updated} comments={run3.comments_fetched}"
    )

    # ───────────────────── Verification ─────────────────────
    _print_section("STEP 5: FTS5 search 'aiter MLA'")
    search = SignalSearch(repo)
    r = search.search(query="aiter MLA", limit=5)
    print(f"Results: {r.get('total', 0)}")
    for s in r.get("results", [])[:3]:
        print(
            f"  [{s.get('github_state', '?')}] #{s.get('source_number')} "
            f"{s.get('title', '')[:80]}"
        )

    _print_section("STEP 6: Search filter (repo+state+labels)")
    r2 = search.search(
        repos=["vllm-project/vllm"], state="open", labels=["rocm"], limit=5
    )
    print(f"Open rocm issues/PRs: {r2.get('total', 0)}")
    for s in r2.get("results", [])[:5]:
        print(
            f"  [{s.get('github_state')}] #{s.get('source_number')} "
            f"{s.get('title', '')[:70]}"
        )

    _print_section("STEP 7: Get detail #39303")
    detail = search.get_detail("github:vllm-project/vllm:issue:39303")
    if detail:
        print(f"Title: {detail['title'][:80]}")
        print(f"Author: {detail['author']}  State: {detail.get('github_state')}")
        print(f"Version: {detail.get('version')}  Hash: {detail.get('content_hash', '')[:16]}…")
        print(f"Body length: {len(detail.get('body', ''))} chars")
        print(f"Comments: {len(detail.get('comments', []))}")
        tags = detail.get("tags")
        if isinstance(tags, str):
            tags = json.loads(tags)
        print(f"Tags: {tags}")

    _print_section("STEP 8: Changes log")
    changes = repo.get_changes(meaningful_only=False, limit=100)
    print(f"Total changes: {len(changes)}")
    by_type: dict[str, int] = {}
    for c in changes:
        by_type[c["change_type"]] = by_type.get(c["change_type"], 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")

    # NEW_COMMENT count — critical for acceptance criterion 4
    nc = [c for c in changes if c["change_type"] == "new_comment"]
    print(f"NEW_COMMENT events detected: {len(nc)}")
    if nc:
        for c in nc[:2]:
            print(f"  • signal={c['signal_id']} old={c['old_value']} new={c['new_value']}")

    _print_section("STEP 9: Cache files")
    issues_dir = cache_dir / "github/vllm-project_vllm/issues"
    pulls_dir = cache_dir / "github/vllm-project_vllm/pulls"
    n_issues = len(list(issues_dir.glob("*.json"))) if issues_dir.exists() else 0
    n_pulls = len(list(pulls_dir.glob("*.json"))) if pulls_dir.exists() else 0
    print(f"Cached issue JSON files: {n_issues}")
    print(f"Cached PR JSON files: {n_pulls}")
    key_cache = issues_dir / "39303.json"
    if key_cache.exists():
        d = json.loads(key_cache.read_text())
        print(f"39303.json present: signal_id={d['signal_id']}")
        print(f"  title: {d['title'][:70]}")
        print(f"  comments in cache: {len(d.get('comments', []))}")
        print(f"  body_token_estimate: {d.get('body_token_estimate')}")
    else:
        print("!! 39303.json NOT FOUND")

    _print_section("STEP 10: DB stats")
    c = sqlite3.connect(db_path)
    for t in (
        "signals",
        "signal_comments",
        "signal_changes",
        "signal_refs",
        "sync_runs",
    ):
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n}")
    distinct_hashes = c.execute(
        "SELECT COUNT(DISTINCT content_hash) FROM signals"
    ).fetchone()[0]
    print(f"  distinct content_hashes: {distinct_hashes}")
    bad_version = c.execute("SELECT COUNT(*) FROM signals WHERE version < 1").fetchone()[0]
    hash_len_ok = c.execute(
        "SELECT COUNT(*) FROM signals WHERE LENGTH(content_hash) = 64"
    ).fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"  signals with version<1: {bad_version}")
    print(f"  signals with 64-char hash: {hash_len_ok}/{total}")
    pr_count = c.execute("SELECT COUNT(*) FROM signals WHERE github_is_pr=1").fetchone()[0]
    issue_count = c.execute("SELECT COUNT(*) FROM signals WHERE github_is_pr=0").fetchone()[0]
    print(f"  issues: {issue_count}  PRs: {pr_count}")

    row_39303 = c.execute(
        "SELECT signal_id, version, github_state, github_comment_count, "
        "LENGTH(content_hash) FROM signals WHERE source_number=39303"
    ).fetchone()
    print(f"  #39303: {row_39303}")
    c.close()

    _print_section("=== ACCEPTANCE SUMMARY ===")
    # Criterion 1: >30 signals (we have 22 — report accurately)
    #              actually 22 (demo data limit) — will note this
    print(f"[1] signals count: {total} (target: >30)")
    print(
        f"[2] all signals have 64-char content_hash: "
        f"{'YES' if hash_len_ok == total else 'NO'} "
        f"({hash_len_ok}/{total}); all version>=1: "
        f"{'YES' if bad_version == 0 else 'NO'}"
    )
    print(f"[3] FTS5 'aiter MLA' matches: {r.get('total', 0)}")
    print(
        f"[4] 2nd sync detected change events: {sum(by_type.values())} "
        f"(incl. NEW_COMMENT: {len(nc)})"
    )
    print(f"[5] signal_changes rows: {sum(by_type.values())}")
    print(f"[6] 39303.json cache present: {'YES' if key_cache.exists() else 'NO'}")
    repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
