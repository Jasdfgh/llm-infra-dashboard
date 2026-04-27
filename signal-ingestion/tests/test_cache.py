"""Unit tests for ``src.storage.cache.JSONCache``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion.models import Comment, Signal, SourceType
from src.storage.cache import JSONCache


def _make_signal(
    *,
    repo: str = "vllm-project/vllm",
    number: int = 39303,
    source_type: SourceType = SourceType.GITHUB_ISSUE,
) -> Signal:
    kind = "issue" if source_type == SourceType.GITHUB_ISSUE else "pr"
    return Signal(
        signal_id=f"github:{repo}:{kind}:{number}",
        source_type=source_type,
        source_url=f"https://github.com/{repo}/issues/{number}",
        source_repo=repo,
        source_number=number,
        title="Test signal",
        body="body text",
        created_at="2025-04-20T00:00:00Z",
        updated_at="2025-04-20T01:00:00Z",
        first_seen_at="2025-04-20T00:00:00Z",
        last_synced_at="2025-04-20T01:00:00Z",
        content_hash="abc123",
    )


def _make_comment(signal_id: str, comment_id: str = "c1") -> Comment:
    return Comment(
        signal_id=signal_id,
        comment_id=comment_id,
        author="alice",
        body="looks good",
        created_at="2025-04-20T02:00:00Z",
    )


# ── 1. write + read round-trip ────────────────────────────────────────────


def test_write_read_roundtrip(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal()
    cache.write(sig)
    data = cache.read(sig.signal_id)
    assert data is not None
    assert data["signal_id"] == sig.signal_id
    assert data["title"] == "Test signal"
    assert data["comments"] == []


# ── 2. path_for_signal produces correct paths for issue vs PR ─────────────


@pytest.mark.parametrize(
    "source_type, expected_segment",
    [
        (SourceType.GITHUB_ISSUE, "issues"),
        (SourceType.GITHUB_PR, "pulls"),
    ],
)
def test_path_for_signal_issue_vs_pr(
    tmp_path: Path, source_type: SourceType, expected_segment: str
) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal(source_type=source_type)
    path = cache.path_for_signal(sig)
    assert expected_segment in path.parts


# ── 3. path format matches convention ─────────────────────────────────────


def test_path_format_convention(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal(repo="vllm-project/vllm", number=39303)
    path = cache.path_for_signal(sig)
    expected = tmp_path / "github" / "vllm-project_vllm" / "issues" / "39303.json"
    assert path == expected


# ── 4. signal_id string parsing → path ────────────────────────────────────


def test_signal_id_string_resolves_to_path(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    path = cache.path_for_signal("github:vllm-project/vllm:issue:39303")
    expected = tmp_path / "github" / "vllm-project_vllm" / "issues" / "39303.json"
    assert path == expected

    pr_path = cache.path_for_signal("github:owner/repo:pr:100")
    assert "pulls" in pr_path.parts
    assert pr_path.name == "100.json"


# ── 5. read non-existent signal_id → None ─────────────────────────────────


def test_read_missing_returns_none(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    assert cache.read("github:fake/repo:issue:9999") is None


# ── 6. exists → True / False ──────────────────────────────────────────────


def test_exists_true_false(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal()
    assert cache.exists(sig.signal_id) is False
    cache.write(sig)
    assert cache.exists(sig.signal_id) is True


# ── 7. write with comments ────────────────────────────────────────────────


def test_write_with_comments(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal()
    comments = [
        _make_comment(sig.signal_id, "c1"),
        _make_comment(sig.signal_id, "c2"),
    ]
    cache.write(sig, comments=comments)
    data = cache.read(sig.signal_id)
    assert data is not None
    assert len(data["comments"]) == 2
    assert data["comments"][0]["comment_id"] == "c1"
    assert data["comments"][1]["comment_id"] == "c2"


# ── 8. atomic write produces valid JSON ───────────────────────────────────


def test_atomic_write_valid_json(tmp_path: Path) -> None:
    cache = JSONCache(tmp_path)
    sig = _make_signal()
    path = cache.write(sig)

    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["signal_id"] == sig.signal_id
