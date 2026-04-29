"""Pydantic models — the single source of truth for data shapes.

All modules import from here. Keep aligned with
D2.1 (Signal envelope), D2.3 (Change event),
D2.4 (Sync run record), D5 (ChangeType enum).

Design notes:
- Signal envelope is source-agnostic; source-specific payloads live in nested
  fields (`github`, `twitter`, `arxiv`). Only one is non-None per signal.
- ChangeType and SourceType use str-enums so they round-trip through JSON / SQL.
- Timestamps are ISO 8601 UTC strings (keep as `str` for DB portability; don't
  auto-convert to datetime to avoid tz-ambiguity).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Enums (str-based for JSON/SQL round-trip safety)
# ============================================================================


class SourceType(str, Enum):
    """Source platform + content kind. See D2.1."""

    GITHUB_ISSUE = "github_issue"
    GITHUB_PR = "github_pr"
    TWEET = "tweet"
    ARXIV_PAPER = "arxiv_paper"
    BLOG_POST = "blog_post"
    ZHIHU_POST = "zhihu_post"


class ChangeType(str, Enum):
    """ChangeEvent taxonomy. See D5 ChangeType enum + D2.3."""

    NEW_SIGNAL = "new_signal"
    STATE_CHANGE = "state_change"
    LABEL_CHANGE = "label_change"
    NEW_COMMENT = "new_comment"
    BODY_EDIT = "body_edit"
    ASSIGNEE_CHANGE = "assignee_change"
    PR_LINKED = "pr_linked"
    PR_MERGED = "pr_merged"
    CLOSED = "closed"
    REOPENED = "reopened"
    COMMENT_COUNT_CHANGE = "comment_count_change"


class SyncMode(str, Enum):
    """Sync run mode. See D3.4."""

    FULL = "full"
    INCREMENTAL = "incremental"
    TARGETED = "targeted"
    DISCOVERY = "discovery"


class SyncStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class RefType(str, Enum):
    """signal_refs.ref_type. See D2.2."""

    MENTIONS = "mentions"
    FIXES = "fixes"
    RELATED = "related"
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


# ============================================================================
# References (extracted from body/comments)
# ============================================================================


class IssueRef(BaseModel):
    """A GitHub issue/PR reference extracted from text.

    `repo` may be None for bare `#NNNN` refs — reconciliation (D5 Step 7e)
    resolves them later against the source repo context.
    """

    model_config = ConfigDict(extra="ignore")

    repo: Optional[str] = None
    number: int
    raw: str


class References(BaseModel):
    """Extracted references bundle. See D2.1 `references` field."""

    model_config = ConfigDict(extra="ignore")

    github_issues: list[IssueRef] = Field(default_factory=list)
    github_prs: list[IssueRef] = Field(default_factory=list)
    external_urls: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)


# ============================================================================
# Source-specific payloads (nested in Signal)
# ============================================================================


class PRChangedFiles(BaseModel):
    """PR file-level changes. See D2.1 github.pr_changed_files."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    additions: int = 0
    deletions: int = 0
    files: list[dict[str, Any]] = Field(default_factory=list)
    # Optional triage classification (tests/fixtures/signals/pr_39616.json convention)
    rocm_specific: list[str] = Field(default_factory=list)
    cuda_specific: list[str] = Field(default_factory=list)
    shared: list[str] = Field(default_factory=list)


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    author: Optional[str] = None
    body_preview: Optional[str] = None
    created_at: Optional[str] = None


class GitHubPayload(BaseModel):
    """GitHub-specific fields nested under Signal.github. See D2.1."""

    model_config = ConfigDict(extra="ignore")

    state: Optional[Literal["open", "closed"]] = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    comment_count: int = 0
    closed_at: Optional[str] = None
    closed_by: Optional[str] = None
    is_pr: bool = False
    pr_merged: Optional[bool] = None
    pr_merged_at: Optional[str] = None
    pr_changed_files: Optional[PRChangedFiles] = None
    pr_review_comments: Optional[list[ReviewComment]] = None


class TwitterPayload(BaseModel):
    """Twitter-specific fields (future). See D2.1 twitter block."""

    model_config = ConfigDict(extra="ignore")

    tweet_id: str
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    author_followers: Optional[int] = None
    is_thread: bool = False
    thread_position: Optional[int] = None
    media_urls: list[str] = Field(default_factory=list)


class ArxivPayload(BaseModel):
    """ArXiv-specific fields (future)."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    abstract: Optional[str] = None
    pdf_url: Optional[str] = None


class Classification(BaseModel):
    """Module 2 write-back. See D2.1 classification block."""

    model_config = ConfigDict(extra="ignore")

    classified_at: str
    gap_ids: list[str] = Field(default_factory=list)
    signal_category: Optional[str] = None
    confidence: Optional[float] = None
    classifier_version: Optional[str] = None


# ============================================================================
# Signal envelope (the main data model)
# ============================================================================


class Signal(BaseModel):
    """Unified signal envelope. See D2.1.

    Deterministic `signal_id` format (D2.1):
      github:  github:{repo}:{issue|pr}:{number}
      twitter: twitter:{tweet_id}
      arxiv:   arxiv:{paper_id}
      blog:    blog:{sha256(canonical_url)[:16]}
      zhihu:   zhihu:{answer_id|article_id}
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    # ── Global identity ──
    signal_id: str
    source_type: SourceType
    source_url: str
    source_repo: Optional[str] = None
    source_number: Optional[int] = None

    # ── Generic content ──
    title: str
    body: str = ""
    body_token_estimate: int = 0
    author: Optional[str] = None
    created_at: str
    updated_at: str

    # ── System metadata ──
    first_seen_at: str
    last_synced_at: str
    content_hash: str
    version: int = 1
    sync_run_id: Optional[str] = None

    # ── Extracted references ──
    references: References = Field(default_factory=References)

    # ── Aggregated tags (GitHub labels + extracted keywords) ──
    tags: list[str] = Field(default_factory=list)

    # ── Source-specific payloads (exactly one non-None per signal) ──
    github: Optional[GitHubPayload] = None
    twitter: Optional[TwitterPayload] = None
    arxiv: Optional[ArxivPayload] = None

    # ── Module 2 write-back (None at ingestion time) ──
    classification: Optional[Classification] = None


# ============================================================================
# Comments (separate table signal_comments)
# ============================================================================


class Comment(BaseModel):
    """One comment on a signal (GitHub issue comment, PR review comment, …).

    See D2.2 signal_comments.
    """

    model_config = ConfigDict(extra="ignore")

    signal_id: str
    comment_id: str
    author: Optional[str] = None
    body: str = ""
    body_token_estimate: int = 0
    created_at: str
    updated_at: Optional[str] = None
    is_bot: bool = False


# ============================================================================
# ChangeEvent (append-only audit log)
# ============================================================================


class ChangeEvent(BaseModel):
    """A single detected change. See D2.3 + D5.

    `old_value` / `new_value` are JSON-serialized strings (not raw values) so
    they round-trip cleanly through the SQLite TEXT column.
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    signal_id: str
    change_type: ChangeType
    changed_at: str  # Source platform time
    detected_at: str  # Our detection time
    old_value: Optional[str] = None  # JSON-encoded
    new_value: Optional[str] = None  # JSON-encoded
    is_meaningful: bool = True
    sync_run_id: Optional[str] = None


# ============================================================================
# Signal refs (cross-signal references)
# ============================================================================


class SignalRef(BaseModel):
    """Relation between two signals (tweet mentions issue, etc).

    `to_signal_id` may be None if target hasn't been ingested yet — filled in
    later by reconciliation step (D4 Step 7e).
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    from_signal_id: str
    to_signal_id: Optional[str] = None
    to_url: str
    ref_type: RefType = RefType.MENTIONS
    created_at: str


# ============================================================================
# Sync run record
# ============================================================================


class ResumeToken(BaseModel):
    """Resume-from-interruption token. See D2.4."""

    model_config = ConfigDict(extra="ignore")

    last_page: Optional[int] = None
    last_since: Optional[str] = None
    last_tweet_id: Optional[str] = None


class SyncRun(BaseModel):
    """One sync execution record. See D2.4."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    id: str  # sync_{repo_slug}_{YYYYMMDD_HHMMSS}
    source_type: str  # "github" | "twitter" | ...
    source_repo: Optional[str] = None
    sync_mode: SyncMode
    started_at: str
    completed_at: Optional[str] = None
    status: SyncStatus = SyncStatus.RUNNING

    signals_total: int = 0
    signals_created: int = 0
    signals_updated: int = 0
    signals_unchanged: int = 0
    comments_fetched: int = 0
    api_calls_used: int = 0

    error_message: Optional[str] = None
    resume_token: Optional[ResumeToken] = None
    config_snapshot: Optional[dict[str, Any]] = None


# ============================================================================
# Raw adapter payloads (before normalization)
# ============================================================================


class RawSignal(BaseModel):
    """Raw data returned by a SourceAdapter, before normalization.

    The `raw_data` dict is the source-platform JSON as-is; `Normalizer`
    transforms it into a `Signal`.
    """

    model_config = ConfigDict(extra="ignore")

    raw_id: str  # Deterministic ID (e.g. "39303" for a GitHub issue)
    source_type: SourceType
    raw_data: dict[str, Any]


class RawComment(BaseModel):
    """Raw comment from a SourceAdapter, before normalization."""

    model_config = ConfigDict(extra="ignore")

    comment_id: str
    author: Optional[str] = None
    body: str = ""
    created_at: str
    updated_at: Optional[str] = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Bot authors (used by change detection + comment classification)
# ============================================================================

BOT_AUTHORS: frozenset[str] = frozenset(
    {
        "github-actions[bot]",
        "dependabot[bot]",
        "stale[bot]",
        "codecov[bot]",
        "mergify[bot]",
        "gemini-code-assist[bot]",
        "copilot-workspace[bot]",
        "codex[bot]",
        "pre-commit-ci[bot]",
        "renovate[bot]",
    }
)


def is_bot_author(login: Optional[str]) -> bool:
    """Return True if the login is a known bot.

    Heuristic: exact match against BOT_AUTHORS, or ends with `[bot]`.
    """
    if not login:
        return False
    lo = login.lower()
    if lo in {b.lower() for b in BOT_AUTHORS}:
        return True
    return lo.endswith("[bot]")


# ============================================================================
# Token estimation (see Appendix B)
# ============================================================================


def estimate_tokens(text: Optional[str]) -> int:
    """Fast token estimator.

    Precise: tiktoken. Fast (MVP, ±20% error):
      - mostly ASCII → len/4
      - mixed/Chinese → len/2
    """
    if not text:
        return 0
    n = len(text)
    if n == 0:
        return 0
    ascii_count = sum(1 for c in text if ord(c) < 128)
    if ascii_count / n > 0.8:
        return n // 4
    return n // 2
