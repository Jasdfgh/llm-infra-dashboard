"""Unit tests for ``src.ingestion.normalizer``.

Constructs GitHub-API-shaped ``raw_data`` dicts from the demo fixtures
(``tests/fixtures/signals/issue_39303.json``, ``pr_39616.json``) and drives them
through ``Normalizer.normalize_signal`` / ``normalize_comment``.

The demo JSON is in an already-processed format (``body_text``, ``comment_count``,
bare ``repo`` etc.), so these tests remap it to the shape that GitHub's REST
API actually returns (``body``, ``comments``, ``repository_url``, nested
``user`` / ``closed_by`` objects). That's the shape ``Normalizer`` is spec'd
against (D4 Step 4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.ingestion.models import (
    RawComment,
    RawSignal,
    SourceType,
)
from src.ingestion.normalizer import Normalizer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


DEMO_DIR = Path(__file__).resolve().parent / "fixtures" / "signals"


@pytest.fixture(scope="module")
def issue_demo() -> dict[str, Any]:
    """Load the raw demo issue payload."""
    return json.loads((DEMO_DIR / "issue_39303.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pr_demo() -> dict[str, Any]:
    """Load the raw demo PR payload."""
    return json.loads((DEMO_DIR / "pr_39616.json").read_text(encoding="utf-8"))


def _issue_raw_from_demo(demo: dict[str, Any]) -> dict[str, Any]:
    """Map the processed demo issue shape to GitHub REST API shape."""
    return {
        "number": demo["number"],
        "title": demo["title"],
        "body": demo["body_text"],
        "state": demo["state"],
        "user": {"login": demo["author"]},
        "labels": [{"name": n} for n in demo["labels"]],
        "assignees": [],
        "comments": demo["comment_count"],
        "created_at": demo["created_at"],
        "updated_at": demo["updated_at"],
        "closed_at": demo.get("closed_at"),
        "closed_by": (
            {"login": demo["closed_by"]} if demo.get("closed_by") else None
        ),
        "html_url": demo["html_url"],
        "repository_url": f"https://api.github.com/repos/{demo['repo']}",
    }


def _pr_raw_from_demo(demo: dict[str, Any]) -> dict[str, Any]:
    """Map the processed demo PR shape to GitHub REST API shape + PR extras."""
    changed = demo.get("changed_files") or {}
    return {
        "number": demo["number"],
        "title": demo["title"],
        "body": demo["body_text"],
        "state": demo["state"],
        "user": {"login": demo["author"]},
        "labels": [{"name": n} for n in demo["labels"]],
        "assignees": [],
        "comments": 0,
        "created_at": demo["created_at"],
        # PR demo has no updated_at; mirror created_at like GitHub would on
        # a fresh PR.
        "updated_at": demo["created_at"],
        "html_url": demo["html_url"],
        "repository_url": f"https://api.github.com/repos/{demo['repo']}",
        # Marker that makes _detect_is_pr treat this as a PR row.
        "pull_request": {"url": f"{demo['html_url']}.diff"},
        "merged": demo.get("merged", False),
        "merged_at": demo.get("merged_at"),
        "changed_files": changed.get("total"),
        "additions": changed.get("additions"),
        "deletions": changed.get("deletions"),
    }


# ---------------------------------------------------------------------------
# normalize_signal: GitHub issue (demo issue_39303)
# ---------------------------------------------------------------------------


def test_normalize_issue_produces_expected_signal_id(
    issue_demo: dict[str, Any],
) -> None:
    """signal_id for a GitHub issue is ``github:{repo}:issue:{number}``."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.signal_id == "github:vllm-project/vllm:issue:39303"


def test_normalize_issue_source_type_and_repo(
    issue_demo: dict[str, Any],
) -> None:
    """source_type → GITHUB_ISSUE; source_repo parsed from repository_url."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.source_type == SourceType.GITHUB_ISSUE
    assert sig.source_repo == "vllm-project/vllm"
    assert sig.source_number == 39303


def test_normalize_issue_github_payload_state_and_labels(
    issue_demo: dict[str, Any],
) -> None:
    """state=='closed', labels contains 'bug' and 'rocm'."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.state == "closed"
    assert "bug" in sig.github.labels
    assert "rocm" in sig.github.labels


def test_normalize_issue_comment_count_is_11(
    issue_demo: dict[str, Any],
) -> None:
    """comment_count mapped from raw ``comments`` field (11 in the demo)."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.comment_count == 11


def test_normalize_issue_body_is_not_truncated(
    issue_demo: dict[str, Any],
) -> None:
    """With ``body_max_chars=0`` (MVP default) body preserves full length."""
    raw_data = _issue_raw_from_demo(issue_demo)
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=raw_data,
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.body == raw_data["body"]
    assert len(sig.body) > 10_000  # demo body is ~13kB


def test_normalize_issue_content_hash_shape_and_version(
    issue_demo: dict[str, Any],
) -> None:
    """content_hash is a 64-char lowercase hex digest; version=1 always."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert len(sig.content_hash) == 64
    assert all(c in "0123456789abcdef" for c in sig.content_hash)
    assert sig.version == 1


def test_normalize_issue_is_not_pr(
    issue_demo: dict[str, Any],
) -> None:
    """An issue row (no ``pull_request`` / ``merged`` keys) is not classified as PR."""
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=_issue_raw_from_demo(issue_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.is_pr is False


def test_normalize_issue_preserves_author_and_timestamps(
    issue_demo: dict[str, Any],
) -> None:
    """author = user.login; created_at / updated_at echoed from raw."""
    raw_data = _issue_raw_from_demo(issue_demo)
    raw = RawSignal(
        raw_id=str(issue_demo["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=raw_data,
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.author == "ghpu"
    assert sig.created_at == raw_data["created_at"]
    assert sig.updated_at == raw_data["updated_at"]


# ---------------------------------------------------------------------------
# normalize_signal: GitHub PR (demo pr_39616)
# ---------------------------------------------------------------------------


def test_normalize_pr_produces_pr_signal_id(pr_demo: dict[str, Any]) -> None:
    """signal_id for a GitHub PR is ``github:{repo}:pr:{number}``."""
    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=_pr_raw_from_demo(pr_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.signal_id == "github:vllm-project/vllm:pr:39616"


def test_normalize_pr_source_type_is_github_pr(
    pr_demo: dict[str, Any],
) -> None:
    """Signal.source_type is GITHUB_PR when raw carries ``pull_request`` key."""
    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=_pr_raw_from_demo(pr_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.source_type == SourceType.GITHUB_PR


def test_normalize_pr_github_is_pr_flag(pr_demo: dict[str, Any]) -> None:
    """github.is_pr == True for PR payloads."""
    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=_pr_raw_from_demo(pr_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.is_pr is True


def test_normalize_pr_captures_merge_state_and_changed_files(
    pr_demo: dict[str, Any],
) -> None:
    """PR extras (merged / merged_at / changed_files) are propagated."""
    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=_pr_raw_from_demo(pr_demo),
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.pr_merged is True
    assert sig.github.pr_merged_at == "2026-04-20T14:44:44Z"
    assert sig.github.pr_changed_files is not None
    assert sig.github.pr_changed_files.total == 2
    assert sig.github.pr_changed_files.additions == 130
    assert sig.github.pr_changed_files.deletions == 58


def test_normalize_pr_merged_from_issues_endpoint(pr_demo: dict[str, Any]) -> None:
    """/issues endpoint: no top-level 'merged', but pull_request.merged_at is set."""
    raw_data = _pr_raw_from_demo(pr_demo)
    # Simulate /issues response: remove top-level merged/merged_at,
    # put merged_at inside pull_request sub-object (as GitHub /issues API does)
    del raw_data["merged"]
    merged_at = raw_data.pop("merged_at")
    raw_data["pull_request"]["merged_at"] = merged_at

    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=raw_data,
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.pr_merged is True
    assert sig.github.pr_merged_at == merged_at


def test_normalize_pr_not_merged_no_false_positive(pr_demo: dict[str, Any]) -> None:
    """/issues endpoint: PR is NOT merged — pull_request.merged_at is null."""
    raw_data = _pr_raw_from_demo(pr_demo)
    del raw_data["merged"]
    raw_data.pop("merged_at", None)
    raw_data["pull_request"]["merged_at"] = None
    raw_data["state"] = "open"

    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=raw_data,
    )
    sig = Normalizer().normalize_signal(raw)
    assert sig.github is not None
    assert sig.github.pr_merged is False
    assert sig.github.pr_merged_at is None


def test_normalize_pr_extracts_references_from_body(
    pr_demo: dict[str, Any],
) -> None:
    """References pass: ROCm/aiter#2720 + HF URLs end up on the Signal."""
    raw = RawSignal(
        raw_id=str(pr_demo["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=_pr_raw_from_demo(pr_demo),
    )
    sig = Normalizer().normalize_signal(raw)

    issue_keys = {(r.repo, r.number) for r in sig.references.github_issues}
    assert ("ROCm/aiter", 2720) in issue_keys
    assert any(
        "huggingface.co/amd/Kimi-K2.5-MXFP4" in u
        for u in sig.references.external_urls
    )


# ---------------------------------------------------------------------------
# normalize_comment
# ---------------------------------------------------------------------------


def test_normalize_comment_passes_through_author() -> None:
    """Comment.author mirrors RawComment.author (adapter's user.login)."""
    raw = RawComment(
        comment_id="c1",
        author="alice",
        body="LGTM",
        created_at="2026-04-20T00:00:00Z",
    )
    comment = Normalizer().normalize_comment(raw, signal_id="sig:1")
    assert comment.signal_id == "sig:1"
    assert comment.author == "alice"
    assert comment.comment_id == "c1"
    assert comment.body == "LGTM"
    assert comment.is_bot is False


def test_normalize_comment_flags_known_bot_author() -> None:
    """``github-actions[bot]`` (in BOT_AUTHORS) → is_bot=True."""
    raw = RawComment(
        comment_id="c2",
        author="github-actions[bot]",
        body="CI run queued.",
        created_at="2026-04-20T00:00:00Z",
    )
    comment = Normalizer().normalize_comment(raw, signal_id="sig:1")
    assert comment.is_bot is True


def test_normalize_comment_flags_generic_bot_suffix() -> None:
    """Any login ending in ``[bot]`` is flagged even if unlisted explicitly."""
    raw = RawComment(
        comment_id="c3",
        author="custom-checker[bot]",
        body="status: green",
        created_at="2026-04-20T00:00:00Z",
    )
    comment = Normalizer().normalize_comment(raw, signal_id="sig:1")
    assert comment.is_bot is True


# ---------------------------------------------------------------------------
# Unsupported source types
# ---------------------------------------------------------------------------


def test_normalize_unsupported_source_type_raises_value_error() -> None:
    """Non-GitHub source types raise ValueError (Twitter/ArXiv planned under D6)."""
    raw = RawSignal(
        raw_id="tweet-123",
        source_type=SourceType.TWEET,
        raw_data={"id": "123", "text": "hello"},
    )
    with pytest.raises(ValueError, match="unsupported source_type"):
        Normalizer().normalize_signal(raw)


def test_normalize_github_payload_missing_number_raises() -> None:
    """A GitHub payload without an int ``number`` must fail fast (not silently mint a bogus id)."""
    raw = RawSignal(
        raw_id="bad",
        source_type=SourceType.GITHUB_ISSUE,
        raw_data={
            "title": "x",
            "repository_url": "https://api.github.com/repos/foo/bar",
            "user": {"login": "alice"},
            "created_at": "2026-04-20T00:00:00Z",
            "updated_at": "2026-04-20T00:00:00Z",
        },
    )
    with pytest.raises(ValueError, match="number"):
        Normalizer().normalize_signal(raw)


def test_normalize_github_payload_missing_repo_raises() -> None:
    """No ``repository_url`` / ``base.repo.full_name`` / ``html_url`` → ValueError."""
    raw = RawSignal(
        raw_id="42",
        source_type=SourceType.GITHUB_ISSUE,
        raw_data={
            "number": 42,
            "title": "x",
            "user": {"login": "alice"},
            "created_at": "2026-04-20T00:00:00Z",
            "updated_at": "2026-04-20T00:00:00Z",
        },
    )
    with pytest.raises(ValueError, match="repo"):
        Normalizer().normalize_signal(raw)
