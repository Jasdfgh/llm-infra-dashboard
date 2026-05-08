"""Source-agnostic normalizer: ``RawSignal → Signal``, ``RawComment → Comment``.

Implements D4 Step 4 (standardization + reference extraction). The Normalizer is the sole translation
layer between source-platform JSON shapes and the unified Signal envelope
(D2.1). All downstream consumers (repository, orchestrator, Module 2 API) see
only ``Signal`` / ``Comment``; they are insulated from the quirks of the
GitHub / Twitter / ArXiv schemas.

Design decisions:

* **No adapter imports.** ``RawSignal.raw_data`` is the full source-platform
  JSON as-is; ``_extract_repo_from_raw`` re-implements the same repo-parsing
  logic as ``GitHubAdapter.make_signal_id`` so Layer 1-C (adapters) and
  Layer 2-D (this) stay decoupled. The small duplication is preferable to
  a cyclic layer dependency.
* **No IO.** The Normalizer is pure transformation; it does not read files,
  touch DBs, or fetch URLs. ``body_max_chars=0`` keeps the full body because
  Module 4 agent context injection needs it.
* **Delegates content hashing to ChangeDetector.** ``compute_content_hash``
  lives in ``change_detector`` (D5) and is called here so the "meaningful
  fields" list has a single definition shared with ``detect_changes``.
* **``version=1`` always.** The Orchestrator increments ``version`` on each
  meaningful change during upsert (D5 Step 5d); the Normalizer has no DB
  context and cannot know the history.
* **Tag aggregation is labels-only for MVP.** D4 Step 4e calls for "GitHub
  labels + keyword extraction from title/body", but keyword extraction
  needs a vocabulary that Module 2 will own. We emit labels (sorted,
  deduped) today and let Module 2 augment tags post-ingestion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.ingestion.change_detector import compute_content_hash
from src.ingestion.models import (
    Comment,
    GitHubPayload,
    PRChangedFiles,
    RawComment,
    RawSignal,
    Signal,
    SourceType,
    estimate_tokens,
    is_bot_author,
)
from src.ingestion.reference_extractor import extract_references


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp with ``+00:00`` suffix (timezone-aware)."""
    return datetime.now(tz=timezone.utc).isoformat()


def _extract_repo_from_raw(raw_data: dict[str, Any]) -> str | None:
    """Derive ``owner/name`` from an ``/issues`` or ``/pulls`` payload.

    Priority (matches ``GitHubAdapter.make_signal_id``):

    1. ``repository_url``: ``https://api.github.com/repos/{owner}/{name}``
       (present on every ``/issues`` and search response).
    2. ``base.repo.full_name``: only on ``/pulls/{n}`` standalone responses.
    3. ``html_url``: last-resort parse from ``github.com/{owner}/{name}/...``.

    Returns ``None`` on unrecognized shapes so the caller can raise a
    descriptive ``ValueError`` with the signal's ``raw_id``.
    """
    repo_url = raw_data.get("repository_url")
    if isinstance(repo_url, str) and "/repos/" in repo_url:
        return repo_url.rsplit("/repos/", 1)[-1].strip("/")

    base = raw_data.get("base")
    if isinstance(base, dict):
        base_repo = base.get("repo")
        if isinstance(base_repo, dict):
            full = base_repo.get("full_name")
            if isinstance(full, str) and full:
                return full

    html = raw_data.get("html_url")
    if isinstance(html, str) and "github.com/" in html:
        tail = html.split("github.com/", 1)[1]
        parts = tail.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"

    return None


def _detect_is_pr(raw_data: dict[str, Any]) -> bool:
    """Same PR detection rule as ``GitHubAdapter.make_signal_id``.

    The ``/issues`` endpoint attaches ``pull_request`` to PR rows. Direct
    ``/pulls/{n}`` responses don't, but they always have ``merged`` /
    ``merged_at``+``issue_url``. Handle both so tests that stub a raw PR
    JSON (without the ``/issues`` fan-out step) still classify correctly.
    """
    if "pull_request" in raw_data:
        return True
    if "merged" in raw_data:
        return True
    if "merged_at" in raw_data and "issue_url" in raw_data:
        return True
    return False


def _maybe_truncate(text: str, limit: int) -> str:
    """Return ``text[:limit]`` when ``limit > 0``, else ``text`` unchanged."""
    if limit and len(text) > limit:
        return text[:limit]
    return text


def _collect_label_names(raw_labels: Any) -> list[str]:
    """Extract label ``name`` strings from the GitHub ``labels`` field.

    GitHub returns labels as ``[{"name": "bug", "color": "..."}]`` on full
    payloads and as plain strings via some compact projections. Accept both
    shapes and drop malformed entries silently (defensive: we never want
    ingestion to fail because a label object is weird).
    """
    out: list[str] = []
    if not isinstance(raw_labels, list):
        return out
    for entry in raw_labels:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                out.append(name)
        elif isinstance(entry, str) and entry:
            out.append(entry)
    return out


def _collect_assignee_logins(raw_assignees: Any) -> list[str]:
    """Extract ``login`` strings from the GitHub ``assignees`` array."""
    out: list[str] = []
    if not isinstance(raw_assignees, list):
        return out
    for entry in raw_assignees:
        if isinstance(entry, dict):
            login = entry.get("login")
            if isinstance(login, str) and login:
                out.append(login)
    return out


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class Normalizer:
    """Convert source-platform raw data to the unified Signal envelope.

    See D2.1 (Signal Envelope JSON Schema) + D4 Step 4 (standardization +
    reference extraction) + D5 (content hashing via ``ChangeDetector``).

    The Normalizer is stateless; construct once per process (or per sync run)
    and call :meth:`normalize_signal` / :meth:`normalize_comment` as needed.

    Args:
        body_max_chars: Truncate signal bodies at this many characters. ``0``
            disables truncation (MVP default — SQLite TEXT column happily
            holds multi-MB bodies and Module 4 context injection needs the
            full text). Non-zero is provided for future cost-sensitive
            scenarios.
        comment_body_max_chars: Same, applied during :meth:`normalize_comment`.
    """

    def __init__(
        self,
        *,
        body_max_chars: int = 0,
        comment_body_max_chars: int = 0,
    ) -> None:
        self._body_max = int(body_max_chars)
        self._comment_body_max = int(comment_body_max_chars)

    # ──────────────────────────── public API ────────────────────────────

    def normalize_signal(
        self,
        raw: RawSignal,
        *,
        sync_run_id: str | None = None,
        first_seen_at: str | None = None,
        last_synced_at: str | None = None,
    ) -> Signal:
        """Dispatch by ``raw.source_type`` and return a normalized ``Signal``.

        Args:
            raw: Source-platform payload wrapped by a ``SourceAdapter``.
            sync_run_id: Tag the produced ``Signal`` with this sync-run id.
            first_seen_at: When we first observed this signal. Defaults to
                ``last_synced_at`` if provided, otherwise to ``now()``.
            last_synced_at: When we pulled this signal this time. Defaults to
                ``now()``. Both timestamps are ISO 8601 UTC with ``+00:00``.

        Returns:
            A fully populated ``Signal`` with ``version=1`` (the Orchestrator
            overrides on upsert: new rows keep 1, meaningful changes bump to
            ``existing.version + 1`` — see D5 Step 5d).

        Raises:
            ValueError: on unsupported ``source_type`` or on GitHub payloads
                that can't be resolved to a ``repo`` / ``number``.
        """
        now_iso = _utc_now_iso()
        synced = last_synced_at or now_iso
        first = first_seen_at or synced

        st = raw.source_type
        if st in (SourceType.GITHUB_ISSUE, SourceType.GITHUB_PR):
            return self._normalize_github(
                raw,
                sync_run_id=sync_run_id,
                first_seen_at=first,
                last_synced_at=synced,
            )

        raise ValueError(
            f"Normalizer.normalize_signal: unsupported source_type={st!r}; "
            "MVP supports GITHUB_ISSUE and GITHUB_PR (Twitter/ArXiv/Blog/"
            "Zhihu normalizers planned under D6)."
        )

    def normalize_comment(
        self,
        raw: RawComment,
        *,
        signal_id: str,
    ) -> Comment:
        """Convert a ``RawComment`` to a ``Comment`` keyed to ``signal_id``.

        Follows D4 Step 6b (bot detection via :func:`is_bot_author` — exact
        match against ``BOT_AUTHORS`` or trailing ``[bot]``).

        Reference extraction over comment bodies is NOT performed here;
        the Orchestrator does that in a separate pass so the emitted
        ``SignalRef`` rows can be written in the same transaction as the
        comment itself.

        Args:
            raw: Source-platform comment payload.
            signal_id: The owning signal's deterministic id.

        Returns:
            A ``Comment`` with ``is_bot`` pre-computed and a token estimate
            for Module 4 budget tracking.
        """
        body = _maybe_truncate(raw.body or "", self._comment_body_max)
        author = raw.author
        return Comment(
            signal_id=signal_id,
            comment_id=str(raw.comment_id),
            author=author,
            body=body,
            body_token_estimate=estimate_tokens(body),
            created_at=raw.created_at or "",
            updated_at=raw.updated_at,
            is_bot=is_bot_author(author),
        )

    # ──────────────────────── per-source normalizers ─────────────────────

    def _normalize_github(
        self,
        raw: RawSignal,
        *,
        sync_run_id: str | None,
        first_seen_at: str,
        last_synced_at: str,
    ) -> Signal:
        """Normalize a GitHub issue or PR. See D2.1 + D4 Step 4."""
        rd = raw.raw_data or {}

        # ── Identity ──
        number = rd.get("number")
        if not isinstance(number, int):
            raise ValueError(
                "GitHub payload missing int 'number' field "
                f"(got {number!r}, raw_id={raw.raw_id!r})"
            )

        repo = _extract_repo_from_raw(rd)
        if not repo:
            raise ValueError(
                "GitHub payload missing repo context (no repository_url, "
                "base.repo.full_name, or parsable html_url). "
                f"raw_id={raw.raw_id!r}, keys={list(rd.keys())}"
            )

        is_pr = _detect_is_pr(rd)
        kind = "pr" if is_pr else "issue"
        signal_id = f"github:{repo}:{kind}:{number}"
        url_kind = "pull" if is_pr else "issues"
        html_url = (
            rd.get("html_url") or f"https://github.com/{repo}/{url_kind}/{number}"
        )

        # ── Content ──
        title = rd.get("title") or ""
        body_raw = rd.get("body") or ""
        body = _maybe_truncate(body_raw, self._body_max)

        user = rd.get("user") if isinstance(rd.get("user"), dict) else {}
        author = (user or {}).get("login") or None

        created_at = rd.get("created_at") or ""
        # updated_at can legitimately equal created_at (no updates yet);
        # fall through to last_synced_at only if GitHub gave us nothing.
        updated_at = rd.get("updated_at") or created_at or last_synced_at

        # ── GitHub payload ──
        labels = _collect_label_names(rd.get("labels"))
        assignees = _collect_assignee_logins(rd.get("assignees"))

        state_raw = rd.get("state")
        state = state_raw if state_raw in ("open", "closed") else None

        closed_at = rd.get("closed_at")
        closed_by_obj = rd.get("closed_by")
        closed_by = (
            closed_by_obj.get("login")
            if isinstance(closed_by_obj, dict)
            else None
        )

        comment_count = int(rd.get("comments") or 0)

        pr_merged: bool | None = None
        pr_merged_at: str | None = None
        pr_changed_files: PRChangedFiles | None = None
        if is_pr:
            pr_merged = bool(rd.get("merged", False))
            pr_merged_at = rd.get("merged_at")

            # /issues endpoint doesn't return top-level "merged"; it stores
            # merged_at inside the pull_request sub-object instead.
            if not pr_merged:
                pr_sub = rd.get("pull_request", {})
                if isinstance(pr_sub, dict) and pr_sub.get("merged_at"):
                    pr_merged = True
                    pr_merged_at = pr_merged_at or pr_sub["merged_at"]
            changed_total_raw = rd.get("changed_files")
            total = changed_total_raw if isinstance(changed_total_raw, int) else 0
            pr_changed_files = PRChangedFiles(
                total=total,
                additions=int(rd.get("additions") or 0),
                deletions=int(rd.get("deletions") or 0),
                # File-level details require a separate /pulls/{n}/files
                # fetch (GitHubAdapter.fetch_pr_files). Normalizer doesn't
                # call it; Orchestrator enriches when needed.
                files=[],
                rocm_specific=[],
                cuda_specific=[],
                shared=[],
            )

        gh_payload = GitHubPayload(
            state=state,
            labels=labels,
            assignees=assignees,
            comment_count=comment_count,
            closed_at=closed_at,
            closed_by=closed_by,
            is_pr=is_pr,
            pr_merged=pr_merged,
            pr_merged_at=pr_merged_at,
            pr_changed_files=pr_changed_files,
            # Review comments require the /pulls/{n}/reviews + /comments
            # endpoints; MVP leaves this at None and lets Orchestrator
            # populate on a deep fetch.
            pr_review_comments=None,
        )

        # ── References ──
        # extract_references strips code blocks before regex matching, so
        # passing body+title concatenated is safe for bodies containing
        # ``#include`` / ``#define`` / shell `#` comments.
        ref_text = f"{title}\n{body}" if body else title
        references = extract_references(ref_text, source_repo=repo)

        # ── Tags: labels only for MVP (see module docstring). ──
        tags = sorted(set(labels))

        # ── Source type enum (use_enum_values=True on Signal will stringify). ──
        source_type_enum = (
            SourceType.GITHUB_PR if is_pr else SourceType.GITHUB_ISSUE
        )

        # ── Content hash (delegated to ChangeDetector). ──
        hash_input: dict[str, Any] = {
            "source_type": source_type_enum.value,
            "title": title,
            "body": body,
            "author": author or "",
            "github": gh_payload.model_dump(),
        }
        content_hash = compute_content_hash(hash_input)

        return Signal(
            signal_id=signal_id,
            source_type=source_type_enum,
            source_url=html_url,
            source_repo=repo,
            source_number=number,
            title=title,
            body=body,
            body_token_estimate=estimate_tokens(body),
            author=author,
            created_at=created_at,
            updated_at=updated_at,
            first_seen_at=first_seen_at,
            last_synced_at=last_synced_at,
            content_hash=content_hash,
            version=1,
            sync_run_id=sync_run_id,
            references=references,
            tags=tags,
            github=gh_payload,
            twitter=None,
            arxiv=None,
            classification=None,
        )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def normalize_signal(
    raw: RawSignal,
    **kwargs: Any,
) -> Signal:
    """Shorthand for ``Normalizer().normalize_signal(raw, **kwargs)``.

    Useful in one-shot scripts and tests where a configured ``Normalizer``
    instance is overkill. Production pipelines should construct one
    ``Normalizer`` and reuse it so truncation limits stay consistent.
    """
    return Normalizer().normalize_signal(raw, **kwargs)
