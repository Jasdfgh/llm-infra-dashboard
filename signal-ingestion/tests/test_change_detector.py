"""Unit tests for ``src.ingestion.change_detector``.

Covers:

* ``compute_content_hash``: determinism, field coverage, exclusions,
  dict vs Signal input.
* ``classify_comment_meaningfulness``: bot authors, auto-generated markers,
  normal human replies.
* ``ChangeDetector.detect``: every emitted ``ChangeType`` per D5
  (NEW_SIGNAL / CLOSED / REOPENED / LABEL_CHANGE / NEW_COMMENT /
  BODY_EDIT / ASSIGNEE_CHANGE / PR_MERGED) + the "multiple changes at once"
  aggregation path.

All tests are pure-function — no DB / network. The ``existing_row`` arg is
constructed as a plain dict exactly as the repository layer would return.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.ingestion.change_detector import (
    ChangeDetector,
    classify_comment_meaningfulness,
    compute_content_hash,
)
from src.ingestion.models import (
    ChangeType,
    Comment,
    GitHubPayload,
    Signal,
    SourceType,
)


# ---------------------------------------------------------------------------
# Fixtures: builders for Signal / existing_row
# ---------------------------------------------------------------------------


def _make_signal(
    *,
    signal_id: str = "github:vllm-project/vllm:issue:42",
    source_type: SourceType = SourceType.GITHUB_ISSUE,
    title: str = "demo title",
    body: str = "demo body",
    author: str | None = "alice",
    state: str = "open",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    comment_count: int = 0,
    closed_at: str | None = None,
    is_pr: bool = False,
    pr_merged: bool | None = None,
    pr_merged_at: str | None = None,
    content_hash: str = "x" * 64,
    version: int = 1,
    created_at: str = "2026-04-10T00:00:00+00:00",
    updated_at: str = "2026-04-15T12:00:00+00:00",
) -> Signal:
    """Build a minimal GitHub ``Signal`` for detector tests."""
    return Signal(
        signal_id=signal_id,
        source_type=source_type,
        source_url=f"https://github.com/vllm-project/vllm/issues/42",
        source_repo="vllm-project/vllm",
        source_number=42,
        title=title,
        body=body,
        author=author,
        created_at=created_at,
        updated_at=updated_at,
        first_seen_at=created_at,
        last_synced_at=updated_at,
        content_hash=content_hash,
        version=version,
        github=GitHubPayload(
            state=state,  # type: ignore[arg-type]
            labels=list(labels or []),
            assignees=list(assignees or []),
            comment_count=comment_count,
            closed_at=closed_at,
            is_pr=is_pr,
            pr_merged=pr_merged,
            pr_merged_at=pr_merged_at,
        ),
    )


def _existing_row_from_signal(signal: Signal, **overrides: Any) -> dict[str, Any]:
    """Build a fake DB row dict matching what SignalRepository returns.

    Uses the ``github_json`` path (priority 1) so tests don't have to
    duplicate the full column matrix. Redundant columns get a coverage
    test of their own below.
    """
    assert signal.github is not None
    gh_payload = signal.github.model_dump()
    row: dict[str, Any] = {
        "signal_id": signal.signal_id,
        "content_hash": signal.content_hash,
        "version": signal.version,
        "title": signal.title,
        "body": signal.body,
        "github_state": gh_payload.get("state"),
        "github_labels": json.dumps(gh_payload.get("labels") or []),
        "github_assignees": json.dumps(gh_payload.get("assignees") or []),
        "github_comment_count": gh_payload.get("comment_count") or 0,
        "github_is_pr": int(bool(gh_payload.get("is_pr"))),
        "github_closed_at": gh_payload.get("closed_at"),
        "github_pr_merged": gh_payload.get("pr_merged"),
        "github_pr_merged_at": gh_payload.get("pr_merged_at"),
        "github_json": json.dumps(gh_payload),
    }
    row.update(overrides)
    return row


# ===========================================================================
# compute_content_hash
# ===========================================================================


def test_hash_is_deterministic_for_same_signal() -> None:
    """Two calls on identical data must return the identical digest."""
    sig = _make_signal()
    assert compute_content_hash(sig) == compute_content_hash(sig)


def test_hash_accepts_both_dict_and_signal() -> None:
    """dict input with the same values produces the same hash as a Signal."""
    sig = _make_signal()
    as_dict = sig.model_dump()
    assert compute_content_hash(sig) == compute_content_hash(as_dict)


def test_hash_has_sha256_shape() -> None:
    """Result is a 64-char lowercase hex digest."""
    h = compute_content_hash(_make_signal())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "changed title"),
        ("body", "totally different body"),
        ("state", "closed"),
        ("labels", ["bug", "rocm"]),
        ("comment_count", 7),
        ("assignees", ["bob"]),
    ],
)
def test_hash_changes_when_meaningful_field_changes(
    field: str, value: Any
) -> None:
    """Every 'meaningful' field change must move the digest."""
    base = _make_signal()
    kwargs: dict[str, Any] = {field: value}
    mutated = _make_signal(**kwargs)
    assert compute_content_hash(base) != compute_content_hash(mutated), (
        f"hash did not change when {field} changed"
    )


def test_hash_ignores_timestamps_and_version() -> None:
    """created_at / updated_at / last_synced_at / version must not affect hash."""
    base = _make_signal()
    drifted = _make_signal(
        created_at="2099-12-31T23:59:59+00:00",
        updated_at="2099-12-31T23:59:59+00:00",
        version=42,
    )
    assert compute_content_hash(base) == compute_content_hash(drifted)


def test_hash_ignores_sync_metadata() -> None:
    """Changing ``last_synced_at`` / ``first_seen_at`` / ``sync_run_id`` is a no-op on hash."""
    base_dict = _make_signal().model_dump()
    drifted_dict = dict(base_dict)
    drifted_dict["last_synced_at"] = "2099-01-01T00:00:00+00:00"
    drifted_dict["first_seen_at"] = "2099-01-01T00:00:00+00:00"
    drifted_dict["sync_run_id"] = "sync_different"
    assert compute_content_hash(base_dict) == compute_content_hash(drifted_dict)


def test_hash_rejects_unsupported_type() -> None:
    """Passing a list / int / None raises ``TypeError``."""
    with pytest.raises(TypeError):
        compute_content_hash("not a signal")  # type: ignore[arg-type]


# ===========================================================================
# classify_comment_meaningfulness
# ===========================================================================


def test_classify_bot_author_is_not_meaningful() -> None:
    """A known bot author flips the comment to non-meaningful."""
    c = Comment(
        signal_id="s",
        comment_id="c",
        author="github-actions[bot]",
        body="Running CI for this PR.",
        created_at="2026-04-20T00:00:00+00:00",
    )
    assert classify_comment_meaningfulness(c) is False


def test_classify_ends_with_bot_suffix_is_not_meaningful() -> None:
    """Any login ending in ``[bot]`` is rejected even if unlisted."""
    c = {"author": "random-custom[bot]", "body": "Triaged."}
    assert classify_comment_meaningfulness(c) is False


def test_classify_signed_off_by_is_not_meaningful() -> None:
    """``Signed-off-by:`` trailers strip a comment of analytical value."""
    c = {
        "author": "alice",
        "body": "LGTM.\n\nSigned-off-by: Alice <alice@example.com>",
    }
    assert classify_comment_meaningfulness(c) is False


def test_classify_html_comment_marker_is_not_meaningful() -> None:
    """An HTML comment opener ``<!-- ...`` signals auto-generated content."""
    c = {
        "author": "alice",
        "body": "<!-- marker: vuln-report -->\nDetails follow",
    }
    assert classify_comment_meaningfulness(c) is False


def test_classify_normal_human_reply_is_meaningful() -> None:
    """A normal human reply with no markers is meaningful."""
    c = {"author": "alice", "body": "I think the root cause is the cache."}
    assert classify_comment_meaningfulness(c) is True


def test_classify_accepts_dict_and_comment_model() -> None:
    """Both dict and ``Comment`` inputs produce the same classification."""
    as_dict = {"author": "alice", "body": "hello"}
    as_model = Comment(
        signal_id="s",
        comment_id="c",
        author="alice",
        body="hello",
        created_at="2026-04-20T00:00:00+00:00",
    )
    assert classify_comment_meaningfulness(as_dict) is True
    assert classify_comment_meaningfulness(as_model) is True


# ===========================================================================
# ChangeDetector.detect — every branch
# ===========================================================================


def test_detect_new_signal_returns_only_new_signal_event() -> None:
    """``existing_row=None`` short-circuits to a single NEW_SIGNAL event."""
    new_sig = _make_signal()
    events = ChangeDetector().detect(new_sig, existing_row=None)
    assert len(events) == 1
    ev = events[0]
    assert ev.change_type == ChangeType.NEW_SIGNAL.value
    assert ev.signal_id == new_sig.signal_id
    assert ev.is_meaningful is True
    assert ev.old_value is None
    assert json.loads(ev.new_value)["signal_id"] == new_sig.signal_id  # type: ignore[arg-type]


def test_detect_closed_event_when_state_transitions_open_to_closed() -> None:
    """open → closed emits a CLOSED event, is_meaningful=True."""
    old = _make_signal(state="open", comment_count=3)
    new = _make_signal(state="closed", comment_count=3)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    close_events = [e for e in events if e.change_type == ChangeType.CLOSED.value]
    assert len(close_events) == 1
    assert close_events[0].is_meaningful is True
    assert json.loads(close_events[0].old_value) == "open"  # type: ignore[arg-type]
    assert json.loads(close_events[0].new_value) == "closed"  # type: ignore[arg-type]


def test_detect_reopened_event_when_state_transitions_closed_to_open() -> None:
    """closed → open emits a REOPENED event."""
    old = _make_signal(state="closed")
    new = _make_signal(state="open")
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    reopen = [e for e in events if e.change_type == ChangeType.REOPENED.value]
    assert len(reopen) == 1
    assert reopen[0].is_meaningful is True


def test_detect_label_change_adds_and_removes() -> None:
    """['bug'] → ['bug', 'rocm'] produces a single LABEL_CHANGE event."""
    old = _make_signal(labels=["bug"])
    new = _make_signal(labels=["bug", "rocm"])
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    label_events = [
        e for e in events if e.change_type == ChangeType.LABEL_CHANGE.value
    ]
    assert len(label_events) == 1
    assert label_events[0].is_meaningful is True
    # new_value is the sorted new-label set, JSON-encoded.
    assert json.loads(label_events[0].new_value) == ["bug", "rocm"]  # type: ignore[arg-type]


def test_detect_new_comment_when_count_grows() -> None:
    """comment_count 5 → 8 emits NEW_COMMENT (is_meaningful=True)."""
    old = _make_signal(comment_count=5)
    new = _make_signal(comment_count=8)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    nc = [e for e in events if e.change_type == ChangeType.NEW_COMMENT.value]
    assert len(nc) == 1
    assert nc[0].is_meaningful is True
    assert json.loads(nc[0].old_value) == 5  # type: ignore[arg-type]
    assert json.loads(nc[0].new_value) == 8  # type: ignore[arg-type]


def test_detect_no_new_comment_when_count_unchanged() -> None:
    """Equal comment_count → no NEW_COMMENT event emitted."""
    old = _make_signal(comment_count=5)
    new = _make_signal(comment_count=5)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)
    assert not any(
        e.change_type == ChangeType.NEW_COMMENT.value for e in events
    )


def test_detect_no_new_comment_when_count_decreases() -> None:
    """Decreasing counts are intentionally ignored (D5: only growth is a new comment)."""
    old = _make_signal(comment_count=8)
    new = _make_signal(comment_count=5)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)
    assert not any(
        e.change_type == ChangeType.NEW_COMMENT.value for e in events
    )


def test_detect_body_edit_large_diff_is_meaningful() -> None:
    """A >50-char body diff marks BODY_EDIT as meaningful."""
    old = _make_signal(body="hi")
    new = _make_signal(body="hi" + "X" * 100)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    be = [e for e in events if e.change_type == ChangeType.BODY_EDIT.value]
    assert len(be) == 1
    assert be[0].is_meaningful is True


def test_detect_body_edit_small_diff_is_not_meaningful() -> None:
    """A ≤50-char body diff is BODY_EDIT, is_meaningful=False (typo tweak)."""
    old = _make_signal(body="original body text here")
    new = _make_signal(body="original body text here with typo")  # +10 chars
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    be = [e for e in events if e.change_type == ChangeType.BODY_EDIT.value]
    assert len(be) == 1
    assert be[0].is_meaningful is False


def test_detect_assignee_change_is_not_meaningful() -> None:
    """Assignee changes are audit-only (is_meaningful=False per D5)."""
    old = _make_signal(assignees=[])
    new = _make_signal(assignees=["alice"])
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    ac = [
        e for e in events if e.change_type == ChangeType.ASSIGNEE_CHANGE.value
    ]
    assert len(ac) == 1
    assert ac[0].is_meaningful is False


def test_detect_pr_merged_event_when_false_to_true() -> None:
    """PR merged False → True emits PR_MERGED is_meaningful=True."""
    old = _make_signal(is_pr=True, pr_merged=False)
    new = _make_signal(
        is_pr=True,
        pr_merged=True,
        pr_merged_at="2026-04-20T14:44:44+00:00",
    )
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    pm = [e for e in events if e.change_type == ChangeType.PR_MERGED.value]
    assert len(pm) == 1
    assert pm[0].is_meaningful is True
    assert json.loads(pm[0].new_value) is True  # type: ignore[arg-type]


def test_detect_no_pr_merged_when_already_merged() -> None:
    """Merged→merged is a no-op (we only emit on the false→true edge)."""
    old = _make_signal(is_pr=True, pr_merged=True)
    new = _make_signal(is_pr=True, pr_merged=True)
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)
    assert not any(
        e.change_type == ChangeType.PR_MERGED.value for e in events
    )


def test_detect_multiple_concurrent_changes() -> None:
    """A single resync can emit multiple ChangeEvents (closed + label + comment)."""
    old = _make_signal(
        state="open",
        labels=["bug"],
        comment_count=3,
        body="original body",
    )
    new = _make_signal(
        state="closed",
        labels=["bug", "rocm"],
        comment_count=8,
        body="original body",  # body untouched
    )
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)

    types = {e.change_type for e in events}
    assert ChangeType.CLOSED.value in types
    assert ChangeType.LABEL_CHANGE.value in types
    assert ChangeType.NEW_COMMENT.value in types
    # body unchanged → no BODY_EDIT
    assert ChangeType.BODY_EDIT.value not in types


def test_detect_no_events_when_nothing_changed() -> None:
    """Identical new+old yields an empty event list (version is bumped elsewhere)."""
    old = _make_signal(
        state="open", labels=["bug"], comment_count=3, body="same body"
    )
    new = _make_signal(
        state="open", labels=["bug"], comment_count=3, body="same body"
    )
    row = _existing_row_from_signal(old)
    events = ChangeDetector().detect(new, row)
    assert events == []


def test_detect_reads_from_redundant_columns_when_github_json_missing() -> None:
    """The detector must fall back to redundant columns when no github_json blob."""
    old = _make_signal(state="open", labels=["bug"])
    new = _make_signal(state="closed", labels=["bug"])
    row = _existing_row_from_signal(old)
    row["github_json"] = None  # force the fallback path

    events = ChangeDetector().detect(new, row)
    close_events = [
        e for e in events if e.change_type == ChangeType.CLOSED.value
    ]
    assert len(close_events) == 1
