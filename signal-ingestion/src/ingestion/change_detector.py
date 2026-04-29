"""Change detection + content hashing for signal sync.

Implements D5 (``compute_content_hash``, ``detect_changes``,
``classify_comment_meaningfulness``).

Module boundaries:

* **No storage imports.** ``ChangeDetector`` consumes ``existing_row`` as a
  plain ``dict`` with DB column names as keys; it knows nothing about the
  repository layer.
* **No adapter imports.** ``Normalizer`` is the only caller of
  ``compute_content_hash`` from the ingestion pipeline; the adapter layer
  does not reach in here.
* **Single source of truth for "meaningful fields".** Both the Normalizer
  (which stamps ``Signal.content_hash``) and ``detect_changes`` (which asks
  "did anything we care about change?") route through
  ``compute_content_hash`` so the two stay in lock-step. Changing the
  covered-field list on one side automatically updates the other.

Design constraints baked into this module:

* ``ChangeEvent.old_value`` / ``new_value`` are ``json.dumps(...)``-encoded
  strings per the Pydantic model (D2.3). Consumers ``json.loads`` them.
* ``is_meaningful`` is the gate the Orchestrator uses to decide whether a
  change triggers ``signals.version += 1`` and deep comment re-scan; we set
  it conservatively per D5 (``assignee_change`` = False, small body typos =
  False, everything else = True).
* Default ``body_edit_meaningful_threshold = 50`` chars matches D5.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Union

from src.ingestion.models import (
    ChangeEvent,
    ChangeType,
    Comment,
    Signal,
    is_bot_author,
)


# ---------------------------------------------------------------------------
# Content hashing (D5)
# ---------------------------------------------------------------------------


def _coerce_to_dict(signal_data: Any) -> dict[str, Any]:
    """Return a plain dict view of either a ``Signal`` or a ``dict``."""
    if isinstance(signal_data, Signal):
        return signal_data.model_dump()
    if isinstance(signal_data, dict):
        return signal_data
    raise TypeError(
        "compute_content_hash: expected Signal or dict, got "
        f"{type(signal_data).__name__}"
    )


def _coerce_nested(value: Any) -> dict[str, Any]:
    """Normalize a nested payload (GitHubPayload, dict, None) to a dict."""
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}


def compute_content_hash(signal_data: Union[dict[str, Any], Signal]) -> str:
    """SHA-256 over the "meaningful" fields of a signal. See D5.

    Included (all sources): ``title``, ``body``, ``author``.
    Included (GitHub): ``state``, ``labels`` (sorted), ``assignees``
        (sorted), ``comment_count``, ``closed_at``, ``is_pr``, ``pr_merged``.
    Included (Twitter): ``likes``, ``retweets``.

    Explicitly excluded: any timestamp, sync metadata (``sync_run_id``,
    ``first_seen_at``, ``last_synced_at``), token estimates, ``version``,
    tags (derived), references (derived).

    The canonical encoding is ``json.dumps(..., sort_keys=True,
    ensure_ascii=False)`` so Chinese/unicode bodies hash byte-stably.

    Args:
        signal_data: A ``Signal`` instance (converted via ``.model_dump()``)
            or a plain ``dict`` with the same structural keys (used by the
            Normalizer *before* it has a full ``Signal`` to hash).

    Returns:
        64-char lowercase hex SHA-256 digest.
    """
    data = _coerce_to_dict(signal_data)

    source_type = data.get("source_type", "")
    if hasattr(source_type, "value"):  # defensive: raw Enum slipped in
        source_type = source_type.value

    meaningful: dict[str, Any] = {
        "title": data.get("title", "") or "",
        "body": data.get("body", "") or "",
        "author": data.get("author", "") or "",
    }

    if source_type in ("github_issue", "github_pr"):
        gh = _coerce_nested(data.get("github"))
        meaningful.update(
            {
                "state": gh.get("state"),
                "labels": sorted(gh.get("labels") or []),
                "assignees": sorted(gh.get("assignees") or []),
                "comment_count": int(gh.get("comment_count") or 0),
                "closed_at": gh.get("closed_at"),
                "is_pr": bool(gh.get("is_pr", False)),
                "pr_merged": gh.get("pr_merged"),
            }
        )
    elif source_type == "tweet":
        tw = _coerce_nested(data.get("twitter"))
        meaningful.update(
            {
                "likes": int(tw.get("likes") or 0),
                "retweets": int(tw.get("retweets") or 0),
            }
        )

    canonical = json.dumps(meaningful, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Comment classification (D5)
# ---------------------------------------------------------------------------

# Auto-generation markers that strip a comment of analytical value (D5).
_COMMENT_AUTO_PATTERNS: tuple[str, ...] = (
    "This comment was marked as",
    "Signed-off-by:",
    "CI passed",
    "CI failed",
    "<!-- ",
    "🔍 Vulnerabilities",
)


def classify_comment_meaningfulness(
    comment: Union[Comment, dict[str, Any]],
) -> bool:
    """Return True when a comment is substantive human prose. See D5.

    Returns False for:

    * Bot authors (matched by :func:`is_bot_author`: exact ``BOT_AUTHORS``
      set or any login ending in ``[bot]``).
    * Auto-generated bodies containing patterns in
      ``_COMMENT_AUTO_PATTERNS`` (sign-off trailers, CI announcements,
      HTML comment openers, dependency-bot vulnerability blocks).

    This is a conservative filter: a human reply inside an otherwise
    auto-generated PR template will be marked non-meaningful because the
    marker pattern wins. The alternative (trying to "subtract" template
    regions) is brittle and not worth the complexity for MVP.
    """
    if isinstance(comment, Comment):
        author = comment.author or ""
        body = comment.body or ""
    else:
        author = comment.get("author") or ""
        body = comment.get("body") or ""

    if is_bot_author(author):
        return False

    for pattern in _COMMENT_AUTO_PATTERNS:
        if pattern in body:
            return False

    return True


# ---------------------------------------------------------------------------
# Change detection (D5 detect_changes)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp with ``+00:00`` suffix (aware)."""
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_existing_gh(existing_row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct a GitHub-payload dict from the stored row.

    Priority order:

    1. ``github_json`` (full serialized GitHubPayload) — the canonical source.
    2. Redundant columns (``github_state``, ``github_labels`` as JSON text,
       ``github_assignees``, ``github_comment_count``, ``github_is_pr``,
       ``github_closed_at``, ``github_pr_merged``, ``github_pr_merged_at``).

    The redundant-column path handles ``existing_row`` shapes produced by
    older migrations or by queries that project only the indexed columns.
    """
    raw = existing_row.get("github_json")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    def _load_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        return []

    return {
        "state": existing_row.get("github_state"),
        "labels": _load_list(existing_row.get("github_labels")),
        "assignees": _load_list(existing_row.get("github_assignees")),
        "comment_count": int(existing_row.get("github_comment_count") or 0),
        "closed_at": existing_row.get("github_closed_at"),
        "is_pr": bool(existing_row.get("github_is_pr") or 0),
        "pr_merged": existing_row.get("github_pr_merged"),
        "pr_merged_at": existing_row.get("github_pr_merged_at"),
    }


class ChangeDetector:
    """Compare a freshly normalized Signal against the stored DB snapshot.

    Emits a list of :class:`ChangeEvent` per D5. The detector is stateless;
    construct once per sync run (or application-wide) and call
    :meth:`detect` for every signal.

    The detector implements the 8 change kinds that MVP cares about:
    ``NEW_SIGNAL``, ``CLOSED``, ``REOPENED``, ``LABEL_CHANGE``,
    ``NEW_COMMENT``, ``BODY_EDIT``, ``ASSIGNEE_CHANGE``, ``PR_MERGED``.
    ``PR_LINKED`` and ``COMMENT_COUNT_CHANGE`` are intentionally deferred
    (task spec: "deferred from MVP").

    Args:
        body_edit_meaningful_threshold: Number of chars. Body edits whose
            absolute length delta is at most this are marked
            ``is_meaningful=False`` (typo fixes / whitespace tweaks that
            shouldn't wake the downstream analysis pipeline). D5 default 50.
    """

    def __init__(self, *, body_edit_meaningful_threshold: int = 50) -> None:
        self._body_threshold = int(body_edit_meaningful_threshold)

    def detect(
        self,
        new_signal: Signal,
        existing_row: dict[str, Any] | None,
        *,
        sync_run_id: str | None = None,
        detected_at: str | None = None,
    ) -> list[ChangeEvent]:
        """Run the full D5 detect_changes algorithm.

        Args:
            new_signal: Fresh ``Signal`` produced by the Normalizer.
            existing_row: Dict-shaped DB row (``SignalRepository.get_by_id``
                result) or ``None`` when this signal has never been stored.
                Expected keys: ``content_hash``, ``version``, ``title``,
                ``body``, ``github_state``, ``github_labels``,
                ``github_is_pr``, ``github_comment_count``, ``github_json``
                (and optionally other ``github_*`` redundant columns).
            sync_run_id: Tag emitted events with this id so downstream
                aggregation can group by sync run.
            detected_at: ISO 8601 UTC. Defaults to ``datetime.now(UTC)``.

        Returns:
            List of ``ChangeEvent`` (possibly empty for "content identical"
            resyncs). A single ``NEW_SIGNAL`` event is returned when
            ``existing_row is None``; no other event types are emitted in
            that case.
        """
        now_iso = detected_at or _utc_now_iso()
        updated_at = new_signal.updated_at or now_iso
        changed_at = updated_at  # D5: source-platform time of the change

        # ── 1. New signal short-circuit ──
        if existing_row is None:
            return [
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.NEW_SIGNAL,
                    changed_at=updated_at,
                    detected_at=now_iso,
                    old_value=None,
                    new_value=json.dumps(
                        {"signal_id": new_signal.signal_id},
                        ensure_ascii=False,
                    ),
                    is_meaningful=True,
                    sync_run_id=sync_run_id,
                )
            ]

        events: list[ChangeEvent] = []

        new_gh = (
            new_signal.github.model_dump() if new_signal.github is not None else {}
        )
        old_gh = _parse_existing_gh(existing_row)

        # ── 2. State: CLOSED / REOPENED ──
        old_state = old_gh.get("state")
        new_state = new_gh.get("state")
        if old_state != new_state and old_state is not None:
            ct = (
                ChangeType.CLOSED
                if new_state == "closed"
                else ChangeType.REOPENED
            )
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ct,
                    changed_at=changed_at,
                    detected_at=now_iso,
                    old_value=json.dumps(old_state, ensure_ascii=False),
                    new_value=json.dumps(new_state, ensure_ascii=False),
                    is_meaningful=True,
                    sync_run_id=sync_run_id,
                )
            )

        # ── 3. Labels ──
        old_labels = set(old_gh.get("labels") or [])
        new_labels = set(new_gh.get("labels") or [])
        if old_labels != new_labels:
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.LABEL_CHANGE,
                    changed_at=changed_at,
                    detected_at=now_iso,
                    old_value=json.dumps(
                        sorted(old_labels), ensure_ascii=False
                    ),
                    new_value=json.dumps(
                        sorted(new_labels), ensure_ascii=False
                    ),
                    is_meaningful=True,
                    sync_run_id=sync_run_id,
                )
            )

        # ── 4. Comment count: only detect growth ──
        # GitHub does not allow deleting comments via user action; a decrease
        # would indicate moderator action or an API glitch. D5 intentionally
        # ignores decreases to avoid false NEW_COMMENT events.
        old_count = int(old_gh.get("comment_count") or 0)
        new_count = int(new_gh.get("comment_count") or 0)
        if new_count > old_count:
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.NEW_COMMENT,
                    changed_at=changed_at,
                    detected_at=now_iso,
                    old_value=json.dumps(old_count, ensure_ascii=False),
                    new_value=json.dumps(new_count, ensure_ascii=False),
                    # Per-comment bot-filter happens during Comment ingestion
                    # (classify_comment_meaningfulness); the count-level event
                    # is always marked meaningful so the Orchestrator knows to
                    # go re-fetch comments.
                    is_meaningful=True,
                    sync_run_id=sync_run_id,
                )
            )

        # ── 5. Body edit ──
        old_body = existing_row.get("body") or ""
        new_body = new_signal.body or ""
        if old_body != new_body:
            diff_len = abs(len(new_body) - len(old_body))
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.BODY_EDIT,
                    changed_at=changed_at,
                    detected_at=now_iso,
                    old_value=json.dumps(
                        f"(len={len(old_body)})", ensure_ascii=False
                    ),
                    new_value=json.dumps(
                        f"(len={len(new_body)}, diff~{diff_len})",
                        ensure_ascii=False,
                    ),
                    is_meaningful=diff_len > self._body_threshold,
                    sync_run_id=sync_run_id,
                )
            )

        # ── 6. Assignees ──
        old_assignees = set(old_gh.get("assignees") or [])
        new_assignees = set(new_gh.get("assignees") or [])
        if old_assignees != new_assignees:
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.ASSIGNEE_CHANGE,
                    changed_at=changed_at,
                    detected_at=now_iso,
                    old_value=json.dumps(
                        sorted(old_assignees), ensure_ascii=False
                    ),
                    new_value=json.dumps(
                        sorted(new_assignees), ensure_ascii=False
                    ),
                    # Management noise per D5 — routed as audit trail, not
                    # as a trigger for downstream re-analysis.
                    is_meaningful=False,
                    sync_run_id=sync_run_id,
                )
            )

        # ── 7. PR merged (false → true only) ──
        old_merged = old_gh.get("pr_merged")
        new_merged = new_gh.get("pr_merged")
        if (not old_merged) and new_merged:
            merge_time = new_gh.get("pr_merged_at") or updated_at
            events.append(
                ChangeEvent(
                    signal_id=new_signal.signal_id,
                    change_type=ChangeType.PR_MERGED,
                    changed_at=merge_time,
                    detected_at=now_iso,
                    old_value=json.dumps(False, ensure_ascii=False),
                    new_value=json.dumps(True, ensure_ascii=False),
                    is_meaningful=True,
                    sync_run_id=sync_run_id,
                )
            )

        return events
