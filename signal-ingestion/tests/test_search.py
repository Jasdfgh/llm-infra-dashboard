"""Unit tests for ``src.storage.search.SignalSearch``.

Uses real SQLite in a ``tmp_path`` fixture — no mocking. The FTS5 virtual
table is exercised end-to-end (insert via repository, query via search).
"""

from __future__ import annotations

import json

import pytest

from src.ingestion.models import (
    ChangeEvent,
    Comment,
    GitHubPayload,
    Signal,
)
from src.storage.database import init_db
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


@pytest.fixture
def repo(db_path):
    r = SignalRepository(db_path)
    yield r
    r.close()


@pytest.fixture
def search(repo):
    return SignalSearch(repo)


@pytest.fixture
def sample_signal():
    return Signal(
        signal_id="github:test/repo:issue:1",
        source_type="github_issue",
        source_url="https://github.com/test/repo/issues/1",
        source_repo="test/repo",
        source_number=1,
        title="Test issue",
        body="Test body content about ROCm and aiter MLA",
        body_token_estimate=20,
        author="alice",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        first_seen_at="2026-04-20T00:00:00Z",
        last_synced_at="2026-04-20T00:00:00Z",
        content_hash="a" * 64,
        tags=["bug", "rocm"],
        github=GitHubPayload(
            state="open", labels=["bug", "rocm"], comment_count=5
        ),
    )


def _make_signal(suffix: str, **overrides) -> Signal:
    defaults = dict(
        signal_id=f"github:test/repo:issue:{suffix}",
        source_type="github_issue",
        source_url=f"https://github.com/test/repo/issues/{suffix}",
        source_repo="test/repo",
        source_number=int(suffix) if suffix.isdigit() else 999,
        title=f"Issue {suffix}",
        body=f"Body for {suffix}",
        body_token_estimate=10,
        author="alice",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        first_seen_at="2026-04-20T00:00:00Z",
        last_synced_at="2026-04-20T00:00:00Z",
        content_hash="b" * 64,
        tags=["test"],
        github=GitHubPayload(state="open", labels=["test"], comment_count=0),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _seed_signals(repo: SignalRepository) -> list[Signal]:
    """Insert a varied set of signals for filter tests. Returns the list."""
    signals = [
        _make_signal(
            "10",
            source_number=10,
            title="ROCm kernel launch failure",
            body="ROCm HIP kernel fails on MI300X with aiter MLA code",
            tags=["bug", "rocm"],
            updated_at="2026-04-10T00:00:00Z",
            last_synced_at="2026-04-10T00:00:00Z",
            content_hash="c" * 64,
            github=GitHubPayload(
                state="open", labels=["bug", "rocm"], comment_count=3
            ),
        ),
        _make_signal(
            "20",
            source_number=20,
            title="CUDA performance regression",
            body="CUDA path slower after v2.1 upgrade",
            tags=["perf", "cuda"],
            updated_at="2026-04-15T00:00:00Z",
            last_synced_at="2026-04-15T00:00:00Z",
            content_hash="d" * 64,
            github=GitHubPayload(
                state="closed", labels=["perf", "cuda"], comment_count=1
            ),
        ),
        _make_signal(
            "30",
            source_number=30,
            source_repo="other/lib",
            title="Memory leak in attention module",
            body="OOM after 1000 iterations with MLA attention",
            tags=["bug", "memory"],
            updated_at="2026-04-18T00:00:00Z",
            last_synced_at="2026-04-18T00:00:00Z",
            content_hash="e" * 64,
            github=GitHubPayload(
                state="open", labels=["bug", "memory"], comment_count=0
            ),
        ),
    ]
    for s in signals:
        repo.upsert_signal(s)
    return signals


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestSearch:
    def test_fts_search_returns_match(self, repo, search):
        _seed_signals(repo)
        result = search.search(query="ROCm kernel")
        assert result["total"] >= 1
        ids = [r["signal_id"] for r in result["results"]]
        assert "github:test/repo:issue:10" in ids

    def test_repo_filter(self, repo, search):
        _seed_signals(repo)
        result = search.search(repos=["other/lib"])
        assert result["total"] == 1
        assert result["results"][0]["source_repo"] == "other/lib"

    def test_state_filter(self, repo, search):
        _seed_signals(repo)
        result = search.search(state="closed")
        assert result["total"] >= 1
        assert all(r["github_state"] == "closed" for r in result["results"])

    def test_labels_filter(self, repo, search):
        _seed_signals(repo)
        result = search.search(labels=["rocm"])
        assert result["total"] >= 1
        for r in result["results"]:
            assert "rocm" in r["github_labels"]

    def test_since_until_filter(self, repo, search):
        _seed_signals(repo)
        result = search.search(
            since="2026-04-14T00:00:00Z",
            until="2026-04-16T00:00:00Z",
        )
        assert result["total"] == 1
        assert result["results"][0]["signal_id"] == "github:test/repo:issue:20"

    def test_combined_filters(self, repo, search):
        _seed_signals(repo)
        result = search.search(
            query="ROCm",
            repos=["test/repo"],
            state="open",
            labels=["rocm"],
        )
        assert result["total"] >= 1
        r = result["results"][0]
        assert r["source_repo"] == "test/repo"
        assert r["github_state"] == "open"

    def test_empty_results(self, repo, search):
        _seed_signals(repo)
        result = search.search(query="nonexistent_xyzzy_keyword")
        assert result["results"] == []
        assert result["total"] == 0

    # -- FTS syntax & semantics regression tests ----------------------------

    def test_fts_or_expands_results(self, repo, search):
        """OR should return the union, not the intersection."""
        _seed_signals(repo)
        only_rocm = search.search(query="ROCm")
        with_or = search.search(query="ROCm OR CUDA")
        assert with_or["total"] >= only_rocm["total"]
        assert with_or["total"] >= 2

    def test_fts_not_excludes(self, repo, search):
        """NOT should narrow results."""
        _seed_signals(repo)
        broad = search.search(query="kernel")
        narrow = search.search(query="kernel NOT CUDA")
        assert narrow["total"] <= broad["total"]

    def test_fts_and_not_normalised(self, repo, search):
        """AND NOT is normalised to NOT (semantic-preserving)."""
        _seed_signals(repo)
        broad = search.search(query="kernel")
        narrow = search.search(query="kernel AND NOT CUDA")
        assert narrow["total"] <= broad["total"]

    def test_fts_phrase_preserved(self, repo, search):
        """Quoted phrase should be passed to FTS5."""
        _seed_signals(repo)
        result = search.search(query='"kernel launch"')
        assert isinstance(result["results"], list)
        assert result["total"] >= 1

    def test_fts_parens_preserved(self, repo, search):
        """Parenthesised grouping should be respected by FTS5."""
        _seed_signals(repo)
        result = search.search(query="(ROCm OR CUDA) AND kernel")
        assert isinstance(result["results"], list)

    def test_fts_column_qualifier_preserved(self, repo, search):
        """Column qualifiers (title:X) should reach FTS5."""
        _seed_signals(repo)
        result = search.search(query="title:ROCm")
        assert isinstance(result["results"], list)
        assert result["total"] >= 1

    def test_fts_column_set_preserved(self, repo, search):
        """Column set syntax {col1 col2}:term should reach FTS5."""
        _seed_signals(repo)
        result = search.search(query="{title body}:ROCm")
        assert isinstance(result["results"], list)
        assert result["total"] >= 1

    def test_fts_negative_column_discriminates(self, repo, search):
        """-title:X must exclude title-only matches."""
        sig_title_only = _make_signal(
            "80", source_number=80,
            title="ROCm in title only",
            body="nothing special here",
            content_hash="t1" * 32,
        )
        sig_body_only = _make_signal(
            "81", source_number=81,
            title="unrelated title",
            body="ROCm appears in body only",
            content_hash="t2" * 32,
        )
        repo.upsert_signal(sig_title_only)
        repo.upsert_signal(sig_body_only)

        result = search.search(query="-title:ROCm")
        ids = {r["signal_id"] for r in result["results"]}
        assert sig_body_only.signal_id in ids
        assert sig_title_only.signal_id not in ids

    def test_fts_prefix_search_preserved(self, repo, search):
        """Prefix search (roc*) should reach FTS5."""
        _seed_signals(repo)
        result = search.search(query="ROC*")
        assert isinstance(result["results"], list)
        assert result["total"] >= 1

    def test_fts_special_chars_cleaned(self, repo, search):
        """Harmful chars cleaned, result valid with no FTS error."""
        _seed_signals(repo)
        for q in ["MI300X/MI355X", "[ROCm]", "foo-bar", "key=value", "C++",
                   "hipcc --offload-arch=gfx942",
                   "ROCm,CUDA", "https://github.com/foo/bar",
                   "key:value", "subtitle:ROCm", "hashtags:ROCm",
                   "nobody:ROCm"]:
            result = search.search(query=q)
            assert isinstance(result["results"], list)
            assert result.get("meta", {}).get("error") is None, (
                f"query={q!r} got FTS error: {result['meta'].get('error')}"
            )

    def test_fts_column_qualifier_case_insensitive(self, repo, search):
        """TITLE:X and Body:X should work like title:X and body:X."""
        _seed_signals(repo)
        lower = search.search(query="title:ROCm")
        upper = search.search(query="TITLE:ROCm")
        mixed = search.search(query="Title:ROCm")
        assert lower["total"] == upper["total"] == mixed["total"]
        assert lower["total"] >= 1

    def test_fts_degenerate_returns_empty_not_full_scan(self, repo, search):
        """Degenerate queries must return 0 results, not a full scan."""
        _seed_signals(repo)
        total_rows = search.search()["total"]
        assert total_rows >= 3

        for q in ["OR OR OR", "AND", "NEAR/3"]:
            result = search.search(query=q)
            assert result["total"] == 0, (
                f"query={q!r} returned {result['total']} rows; "
                f"expected 0 (not a full scan of {total_rows})"
            )

    def test_fts_unbalanced_quote_handled(self, repo, search):
        """Unbalanced quote should be auto-closed."""
        _seed_signals(repo)
        result = search.search(query='"ROCm kernel')
        assert isinstance(result["results"], list)
        assert result["total"] >= 1

    def test_fts_invalid_syntax_returns_error_in_meta(self, repo, search):
        """Invalid FTS5 syntax returns structured error, not exception."""
        _seed_signals(repo)
        for q in ["NOT ROCm", "ROCm OR NOT CUDA"]:
            result = search.search(query=q)
            assert result["total"] == 0
            assert "error" in result["meta"]

    def test_fts_not_discriminates(self, repo, search):
        """NOT must actually exclude, not just narrow by count."""
        sig_with = _make_signal(
            "90", source_number=90,
            title="kernel CUDA test",
            body="kernel CUDA performance",
            content_hash="n1" * 32,
        )
        sig_without = _make_signal(
            "91", source_number=91,
            title="kernel only test",
            body="kernel performance without GPU vendor",
            content_hash="n2" * 32,
        )
        repo.upsert_signal(sig_with)
        repo.upsert_signal(sig_without)

        result = search.search(query="kernel NOT CUDA")
        ids = {r["signal_id"] for r in result["results"]}
        assert sig_without.signal_id in ids
        assert sig_with.signal_id not in ids

    def test_fts_initial_token_anchor(self, repo, search):
        """title:^term must only match titles STARTING with that term."""
        sig_start = _make_signal(
            "92", source_number=92,
            title="kernel launch failure",
            body="some body",
            content_hash="a1" * 32,
        )
        sig_middle = _make_signal(
            "93", source_number=93,
            title="fix kernel launch issue",
            body="some body",
            content_hash="a2" * 32,
        )
        repo.upsert_signal(sig_start)
        repo.upsert_signal(sig_middle)

        result = search.search(query="title:^kernel")
        ids = {r["signal_id"] for r in result["results"]}
        assert sig_start.signal_id in ids
        assert sig_middle.signal_id not in ids

    def test_fts_near_proximity(self, repo, search):
        """NEAR(a b, 1) must only match when tokens are adjacent."""
        sig_adjacent = _make_signal(
            "94", source_number=94,
            body="kernel launch failure",
            content_hash="nr1" * 32,
        )
        sig_far = _make_signal(
            "95", source_number=95,
            body="kernel is not related to launch at all",
            content_hash="nr2" * 32,
        )
        repo.upsert_signal(sig_adjacent)
        repo.upsert_signal(sig_far)

        result = search.search(query="NEAR(kernel launch, 1)")
        ids = {r["signal_id"] for r in result["results"]}
        assert sig_adjacent.signal_id in ids
        assert sig_far.signal_id not in ids

    def test_fts_phrase_content_not_modified_by_sanitizer(self, repo, search):
        """Quoted phrase content must not be altered by sanitizer.

        Regression: earlier sanitizers applied NEAR/N stripping and
        AND NOT → NOT globally, corrupting phrase content.
        """
        sig_and_not = _make_signal(
            "70",
            source_number=70,
            body="foo AND NOT bar",
            content_hash="p" * 64,
        )
        sig_not = _make_signal(
            "71",
            source_number=71,
            body="foo NOT bar",
            content_hash="q" * 64,
        )
        repo.upsert_signal(sig_and_not)
        repo.upsert_signal(sig_not)

        result = search.search(query='"foo AND NOT bar"')
        ids = [r["signal_id"] for r in result["results"]]
        assert sig_and_not.signal_id in ids
        assert sig_not.signal_id not in ids


# ---------------------------------------------------------------------------
# get_detail()
# ---------------------------------------------------------------------------


class TestGetDetail:
    def test_existing_signal_returns_parsed_json(self, repo, search, sample_signal):
        repo.upsert_signal(sample_signal)
        detail = search.get_detail(sample_signal.signal_id)
        assert detail is not None
        assert isinstance(detail["tags"], list)
        assert isinstance(detail["github_labels"], list)
        assert isinstance(detail["github_json"], dict)
        refs = detail.get("references_json")
        assert refs is None or isinstance(refs, dict)

    def test_detail_with_comments_sorted(self, repo, search, sample_signal):
        repo.upsert_signal(sample_signal)
        repo.upsert_comments(
            [
                Comment(
                    signal_id=sample_signal.signal_id,
                    comment_id="c_late",
                    author="bob",
                    body="Late comment",
                    created_at="2026-04-10T00:00:00Z",
                ),
                Comment(
                    signal_id=sample_signal.signal_id,
                    comment_id="c_early",
                    author="carol",
                    body="Early comment",
                    created_at="2026-04-02T00:00:00Z",
                ),
            ]
        )
        detail = search.get_detail(sample_signal.signal_id)
        comments = detail["comments"]
        assert len(comments) == 2
        assert comments[0]["comment_id"] == "c_early"
        assert comments[1]["comment_id"] == "c_late"

    def test_nonexistent_signal(self, repo, search):
        assert search.get_detail("does:not:exist") is None


# ---------------------------------------------------------------------------
# get_feed()
# ---------------------------------------------------------------------------


class TestGetFeed:
    def test_unclassified_only(self, repo, search):
        """classified=False returns only signals where classification_json IS NULL."""
        sig_unclassified = _make_signal("40", source_number=40, content_hash="f" * 64)
        sig_classified = _make_signal("50", source_number=50, content_hash="g" * 64)
        repo.upsert_signal(sig_unclassified)
        repo.upsert_signal(sig_classified)
        repo.update_classification(
            sig_classified.signal_id,
            gap_ids=["GAP-001"],
            signal_category="bug",
            confidence=0.9,
            classifier_version="v0.1",
        )

        feed = search.get_feed(since="2026-01-01T00:00:00Z", classified=False)
        ids = [s["signal_id"] for s in feed["signals"]]
        assert sig_unclassified.signal_id in ids
        assert sig_classified.signal_id not in ids

    def test_body_preview_max_200_chars(self, repo, search):
        long_body = "x" * 500
        sig = _make_signal("60", source_number=60, body=long_body, content_hash="h" * 64)
        repo.upsert_signal(sig)

        feed = search.get_feed(since="2026-01-01T00:00:00Z")
        item = next(s for s in feed["signals"] if s["signal_id"] == sig.signal_id)
        assert len(item["body_preview"]) <= 200

    def test_recent_changes_attached(self, repo, search, sample_signal):
        repo.upsert_signal(sample_signal)
        repo.append_changes(
            [
                ChangeEvent(
                    signal_id=sample_signal.signal_id,
                    change_type="state_change",
                    changed_at="2026-04-20T01:00:00Z",
                    detected_at="2026-04-20T01:00:00Z",
                    old_value='"open"',
                    new_value='"closed"',
                    is_meaningful=True,
                ),
            ]
        )

        feed = search.get_feed(since="2026-04-01T00:00:00Z")
        item = next(
            s for s in feed["signals"] if s["signal_id"] == sample_signal.signal_id
        )
        assert "recent_changes" in item
        assert len(item["recent_changes"]) == 1

    def test_total_token_estimate_in_meta(self, repo, search, sample_signal):
        repo.upsert_signal(sample_signal)
        feed = search.get_feed(since="2026-01-01T00:00:00Z")
        assert "total_token_estimate" in feed["meta"]
        assert feed["meta"]["total_token_estimate"] >= sample_signal.body_token_estimate
