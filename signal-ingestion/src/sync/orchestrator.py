"""SyncOrchestrator — wires adapter → normalizer → change_detector → repository.

Implements the end-to-end pipeline described in
D4 (incremental update pipeline) + D3.4 (code-level interfaces).

Responsibilities:
  1. Open a SyncRun record, resolve `since` for incremental mode.
  2. Stream raw signals from the adapter (discover / fetch_detail / search).
  3. Normalize each raw → Signal, detect changes vs existing DB row.
  4. Deep-fetch comments when comment_count changed (D4 Step 6).
  5. Write per-signal transactions (D4 Step 7): signals + comments + changes + refs.
  6. Write JSON cache file (D4 Step 8).
  7. Reconcile cross-signal refs (D4 Step 7e).
  8. Close SyncRun with final stats.

This module contains no CLI parsing or I/O setup — `scripts/sync_github.py`
is the thin CLI wrapper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.ingestion.rate_limiter import TokenPool

from src.ingestion.adapters.base import SourceConfig
from src.ingestion.adapters.github_adapter import GitHubAdapter
from src.ingestion.change_detector import ChangeDetector
from src.ingestion.models import (
    ChangeEvent,
    Comment,
    RefType,
    ResumeToken,
    Signal,
    SignalRef,
    SourceType,
    SyncMode,
    SyncRun,
    SyncStatus,
)
from src.ingestion.normalizer import Normalizer
from src.ingestion.reference_extractor import extract_references
from src.storage.cache import JSONCache
from src.storage.repository import SignalRepository

logger = logging.getLogger(__name__)


# ============================================================================
# Result / progress reporting
# ============================================================================


@dataclass
class SyncProgress:
    """Live progress that the CLI can print while a sync is running."""

    signals_seen: int = 0
    signals_new: int = 0
    signals_updated: int = 0
    signals_unchanged: int = 0
    comments_fetched: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signals_seen": self.signals_seen,
            "signals_new": self.signals_new,
            "signals_updated": self.signals_updated,
            "signals_unchanged": self.signals_unchanged,
            "comments_fetched": self.comments_fetched,
            "failures": list(self.failures),
        }


# ============================================================================
# Orchestrator
# ============================================================================


def _now_iso() -> str:
    """Current UTC time as ISO 8601 with `Z` suffix for GitHub parity."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id(source_repo: Optional[str]) -> str:
    """Generate the sync_runs.id. Format: sync_{repo_slug}_{YYYYMMDD_HHMMSS_NNNNNN}.

    Microsecond suffix guarantees uniqueness across back-to-back runs.
    """
    repo_slug = (source_repo or "unknown").replace("/", "_")
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond:06d}"
    return f"sync_{repo_slug}_{ts}"


class SyncOrchestrator:
    """Coordinate a single sync run end-to-end.

    Component injection over construction: accepts pre-built adapter / repo /
    cache so tests can mock them independently. The CLI factory helper at the
    bottom of this module builds a default wiring.
    """

    def __init__(
        self,
        *,
        adapter: GitHubAdapter,
        normalizer: Normalizer,
        change_detector: ChangeDetector,
        repository: SignalRepository,
        cache: JSONCache,
    ) -> None:
        self.adapter = adapter
        self.normalizer = normalizer
        self.change_detector = change_detector
        self.repository = repository
        self.cache = cache

    # ─── Public entry point ──────────────────────────────────────────────

    async def run(
        self,
        *,
        source_repo: str,
        sync_mode: SyncMode = SyncMode.INCREMENTAL,
        labels: Optional[list[str]] = None,
        state: str = "all",
        since: Optional[str] = None,
        target_numbers: Optional[list[int]] = None,
        include_comments: bool = True,
        max_comments_per_issue: int = 100,
        max_pages: Optional[int] = None,
        deep_scan_on_change: bool = True,
        progress_callback: Optional[Any] = None,
    ) -> SyncRun:
        """Execute a sync run. Returns the final SyncRun record.

        `progress_callback`: optional `Callable[[SyncProgress, str], None]`
        invoked after each signal with (progress, last_signal_id).
        """
        run_id = _run_id(source_repo)
        started_at = _now_iso()
        run = SyncRun(
            id=run_id,
            source_type="github",
            source_repo=source_repo,
            sync_mode=sync_mode,
            started_at=started_at,
            status=SyncStatus.RUNNING,
            config_snapshot={
                "labels": labels or [],
                "state": state,
                "since": since,
                "target_numbers": target_numbers,
                "include_comments": include_comments,
                "max_comments_per_issue": max_comments_per_issue,
                "max_pages": max_pages,
            },
        )
        self.repository.create_sync_run(run)
        logger.info(
            "[%s] sync started: repo=%s mode=%s labels=%s",
            run_id,
            source_repo,
            sync_mode.value if hasattr(sync_mode, "value") else sync_mode,
            labels,
        )

        # Resolve `since` for incremental mode
        effective_since = self._resolve_since(sync_mode, source_repo, since)
        if effective_since:
            logger.info("[%s] incremental since=%s", run_id, effective_since)

        progress = SyncProgress()
        status = SyncStatus.COMPLETED
        error_message: Optional[str] = None
        resume_token: Optional[ResumeToken] = None

        try:
            if sync_mode == SyncMode.TARGETED:
                if not target_numbers:
                    raise ValueError("sync_mode=TARGETED requires target_numbers")
                await self._run_targeted(
                    source_repo=source_repo,
                    target_numbers=target_numbers,
                    run_id=run_id,
                    include_comments=include_comments,
                    max_comments_per_issue=max_comments_per_issue,
                    deep_scan_on_change=deep_scan_on_change,
                    progress=progress,
                    progress_callback=progress_callback,
                )
            else:
                await self._run_list(
                    source_repo=source_repo,
                    labels=labels,
                    state=state,
                    since=effective_since,
                    max_pages=max_pages,
                    run_id=run_id,
                    include_comments=include_comments,
                    max_comments_per_issue=max_comments_per_issue,
                    deep_scan_on_change=deep_scan_on_change,
                    progress=progress,
                    progress_callback=progress_callback,
                )

            # Reconcile cross-signal refs (D4 Step 7e)
            try:
                reconciled = self.repository.reconcile_refs()
                if reconciled:
                    logger.info("[%s] reconciled %d refs", run_id, reconciled)
            except Exception as exc:
                logger.warning("[%s] reconcile_refs failed: %s", run_id, exc)

        except KeyboardInterrupt:
            status = SyncStatus.PARTIAL
            error_message = "interrupted by user"
            logger.warning("[%s] interrupted; marking partial", run_id)
        except Exception as exc:
            status = SyncStatus.FAILED
            error_message = f"{type(exc).__name__}: {exc}"
            logger.exception("[%s] fatal error", run_id)
        finally:
            if progress.failures and status == SyncStatus.COMPLETED:
                status = SyncStatus.PARTIAL
                error_message = f"{len(progress.failures)} signal(s) failed"

            api_calls = getattr(self.adapter.rate_limiter, "total_api_calls", 0)
            self.repository.update_sync_run(
                run_id,
                status=status,
                completed_at=_now_iso(),
                signals_total=progress.signals_seen,
                signals_created=progress.signals_new,
                signals_updated=progress.signals_updated,
                signals_unchanged=progress.signals_unchanged,
                comments_fetched=progress.comments_fetched,
                api_calls_used=api_calls,
                error_message=error_message,
                resume_token=resume_token,
            )
            logger.info(
                "[%s] sync %s: total=%d new=%d updated=%d unchanged=%d comments=%d api=%d",
                run_id,
                status.value if hasattr(status, "value") else status,
                progress.signals_seen,
                progress.signals_new,
                progress.signals_updated,
                progress.signals_unchanged,
                progress.comments_fetched,
                api_calls,
            )

        # Return the final record for caller inspection
        return SyncRun(
            id=run_id,
            source_type="github",
            source_repo=source_repo,
            sync_mode=sync_mode,
            started_at=started_at,
            completed_at=_now_iso(),
            status=status,
            signals_total=progress.signals_seen,
            signals_created=progress.signals_new,
            signals_updated=progress.signals_updated,
            signals_unchanged=progress.signals_unchanged,
            comments_fetched=progress.comments_fetched,
            api_calls_used=api_calls,
            error_message=error_message,
        )

    # ─── Mode dispatch ───────────────────────────────────────────────────

    async def _run_list(
        self,
        *,
        source_repo: str,
        labels: Optional[list[str]],
        state: str,
        since: Optional[str],
        max_pages: Optional[int],
        run_id: str,
        include_comments: bool,
        max_comments_per_issue: int,
        deep_scan_on_change: bool,
        progress: SyncProgress,
        progress_callback: Optional[Any],
    ) -> None:
        """Full / incremental mode — stream through `adapter.discover()`."""
        cfg = SourceConfig(
            source_type=SourceType.GITHUB_ISSUE,
            params={
                "repo": source_repo,
                "labels": labels or [],
                "state": state,
                "per_page": 100,
                "max_pages": max_pages,
            },
        )
        async for raw in self.adapter.discover(cfg, since=since):
            try:
                await self._process_one(
                    raw,
                    source_repo=source_repo,
                    run_id=run_id,
                    include_comments=include_comments,
                    max_comments_per_issue=max_comments_per_issue,
                    deep_scan_on_change=deep_scan_on_change,
                    progress=progress,
                )
                if progress_callback:
                    progress_callback(progress, raw.raw_id)
            except Exception as exc:
                sid = self._preview_signal_id(raw, source_repo)
                logger.exception("[%s] failed to process %s", run_id, sid)
                progress.failures.append((sid, f"{type(exc).__name__}: {exc}"))

    async def _run_targeted(
        self,
        *,
        source_repo: str,
        target_numbers: list[int],
        run_id: str,
        include_comments: bool,
        max_comments_per_issue: int,
        deep_scan_on_change: bool,
        progress: SyncProgress,
        progress_callback: Optional[Any],
    ) -> None:
        """Targeted mode — fetch specific issue/PR numbers directly."""
        for number in target_numbers:
            try:
                raw = await self.adapter.fetch_detail(str(number), repo=source_repo)
                if raw is None:
                    logger.warning("[%s] #%d not found in %s", run_id, number, source_repo)
                    continue
                await self._process_one(
                    raw,
                    source_repo=source_repo,
                    run_id=run_id,
                    include_comments=include_comments,
                    max_comments_per_issue=max_comments_per_issue,
                    deep_scan_on_change=deep_scan_on_change,
                    progress=progress,
                )
                if progress_callback:
                    progress_callback(progress, str(number))
            except Exception as exc:
                sid = f"github:{source_repo}:*:{number}"
                logger.exception("[%s] failed to process %s", run_id, sid)
                progress.failures.append((sid, f"{type(exc).__name__}: {exc}"))

    # ─── Per-signal pipeline ─────────────────────────────────────────────

    async def _process_one(
        self,
        raw: Any,  # RawSignal — using Any to avoid unused import
        *,
        source_repo: str,
        run_id: str,
        include_comments: bool,
        max_comments_per_issue: int,
        deep_scan_on_change: bool,
        progress: SyncProgress,
    ) -> None:
        """Normalize + detect changes + persist + cache. Per-signal transaction."""
        progress.signals_seen += 1

        # 1. Normalize
        signal = self.normalizer.normalize_signal(raw, sync_run_id=run_id)

        # 2. Look up existing row to decide path
        existing = self.repository.get_by_id(signal.signal_id)
        is_new = existing is None
        content_changed = is_new or existing["content_hash"] != signal.content_hash

        # Preserve original first_seen_at on updates
        if existing is not None:
            signal = signal.model_copy(update={"first_seen_at": existing["first_seen_at"]})

        # 3. Fast path: content unchanged → touch last_synced_at only, no events
        if not content_changed:
            # Preserve existing version (no bump)
            existing_version = existing["version"]
            signal_touch = signal.model_copy(update={"version": existing_version})
            self.repository.upsert_signal(
                signal_touch, version_override=existing_version
            )
            progress.signals_unchanged += 1
            return

        # 4. Content changed → detect events
        events: list[ChangeEvent] = self.change_detector.detect(
            signal, existing_row=existing, sync_run_id=run_id
        )

        # 5. Deep scan decision — fetch comments if comment_count changed or is_new
        comments_to_write: list[Comment] = []
        refs_from_comments: list[SignalRef] = []
        if include_comments and deep_scan_on_change:
            should_fetch = is_new or _has_comment_change(events)
            if should_fetch:
                comments_to_write, refs_from_comments = await self._fetch_and_normalize_comments(
                    raw,
                    signal=signal,
                    existing=existing,
                    max_comments=max_comments_per_issue,
                )
                progress.comments_fetched += len(comments_to_write)

        # 6. Extract refs from body (signal-level)
        refs_from_body = self._refs_from_body(signal)
        all_refs = refs_from_body + refs_from_comments

        # 7. Persist in a single transaction (D4 Step 7)
        self._persist_atomic(
            signal=signal,
            comments=comments_to_write,
            events=events,
            refs=all_refs,
        )

        # 8. Cache file (D4 Step 8) — after DB commit
        try:
            all_comments_rows = self.repository.get_comments(signal.signal_id)
            cache_comments = [
                Comment.model_validate(row) for row in all_comments_rows
            ] if all_comments_rows else comments_to_write
            self.cache.write(signal, comments=cache_comments or None)
        except Exception as exc:
            logger.warning("cache write failed for %s: %s", signal.signal_id, exc)

        if is_new:
            progress.signals_new += 1
        else:
            progress.signals_updated += 1

    def _persist_atomic(
        self,
        *,
        signal: Signal,
        comments: list[Comment],
        events: list[ChangeEvent],
        refs: list[SignalRef],
    ) -> None:
        """Atomic per-signal write (D4 Step 7 + C1).

        Wraps the four writes in a single ``repository.transaction()`` so a
        failure inside rolls back the whole signal; but since the orchestrator
        loop catches exceptions per-signal, other signals keep making progress.
        """
        with self.repository.transaction():
            self.repository.upsert_signal(signal)
            if comments:
                self.repository.upsert_comments(comments)
            if events:
                self.repository.append_changes(events)
            if refs:
                self.repository.upsert_refs(refs)

    # ─── Comment fetching + ref extraction ───────────────────────────────

    async def _fetch_and_normalize_comments(
        self,
        raw: Any,
        *,
        signal: Signal,
        existing: Optional[dict[str, Any]],
        max_comments: int,
    ) -> tuple[list[Comment], list[SignalRef]]:
        """Fetch (incremental when possible) and normalize comments."""
        # Incremental: if we already have this signal, only ask for comments
        # updated after the last sync. GitHub's /issues/{n}/comments supports
        # ?since= which filters by updated_at.
        since = None
        if existing is not None:
            since = existing.get("last_synced_at")

        raw_comments = await self.adapter.fetch_comments(
            str(signal.source_number),
            repo=signal.source_repo or "",
            since=since,
            max_comments=max_comments,
        )

        comments = [
            self.normalizer.normalize_comment(rc, signal_id=signal.signal_id)
            for rc in raw_comments
        ]

        # Extract refs from each comment body (D4 Step 4c applied to comments)
        refs: list[SignalRef] = []
        detected_at = _now_iso()
        for c in comments:
            if not c.body:
                continue
            extracted = extract_references(c.body, source_repo=signal.source_repo)
            refs.extend(_refs_to_signal_refs(
                signal.signal_id, extracted, detected_at
            ))
        return comments, refs

    def _refs_from_body(self, signal: Signal) -> list[SignalRef]:
        """References extracted at normalization time live in
        signal.references (populated by Normalizer via ReferenceExtractor).
        Convert them into SignalRef rows for `signal_refs` table.
        """
        return _refs_to_signal_refs(
            signal.signal_id, signal.references, _now_iso()
        )

    # ─── Helpers ─────────────────────────────────────────────────────────

    def _resolve_since(
        self,
        sync_mode: SyncMode,
        source_repo: str,
        explicit_since: Optional[str],
    ) -> Optional[str]:
        """Determine effective `since` for the adapter call.

        Priority: explicit_since > last successful sync (incremental) > None.
        FULL mode always returns None regardless.
        """
        if sync_mode == SyncMode.FULL:
            return None
        if explicit_since:
            return explicit_since
        if sync_mode == SyncMode.INCREMENTAL:
            last = self.repository.get_last_successful_sync(source_repo)
            if last and last.get("completed_at"):
                return last["completed_at"]
        return None

    @staticmethod
    def _preview_signal_id(raw: Any, source_repo: str) -> str:
        """Best-effort signal_id reconstruction for error logs."""
        number = raw.raw_data.get("number") if hasattr(raw, "raw_data") else None
        kind = "pr" if (hasattr(raw, "raw_data") and raw.raw_data.get("pull_request")) else "issue"
        return f"github:{source_repo}:{kind}:{number}"


# ============================================================================
# Module-level helpers
# ============================================================================


def _has_comment_change(events: list[ChangeEvent]) -> bool:
    """Return True if the change set implies a comment count delta."""
    for e in events:
        ct = e.change_type.value if hasattr(e.change_type, "value") else e.change_type
        if ct in ("new_comment", "comment_count_change"):
            return True
    return False


def _refs_to_signal_refs(
    from_signal_id: str,
    extracted: Any,  # References
    detected_at: str,
) -> list[SignalRef]:
    """Flatten References → list[SignalRef] rows for signal_refs table."""
    out: list[SignalRef] = []
    # GitHub issue refs
    for r in extracted.github_issues:
        if r.repo and r.number:
            url = f"https://github.com/{r.repo}/issues/{r.number}"
        else:
            # bare #N without known repo — skip for now (reconciliation needs URL)
            continue
        out.append(
            SignalRef(
                from_signal_id=from_signal_id,
                to_url=url,
                ref_type=RefType.MENTIONS,
                created_at=detected_at,
            )
        )
    for r in extracted.github_prs:
        if r.repo and r.number:
            url = f"https://github.com/{r.repo}/pull/{r.number}"
        else:
            continue
        out.append(
            SignalRef(
                from_signal_id=from_signal_id,
                to_url=url,
                ref_type=RefType.MENTIONS,
                created_at=detected_at,
            )
        )
    for url in extracted.external_urls:
        out.append(
            SignalRef(
                from_signal_id=from_signal_id,
                to_url=url,
                ref_type=RefType.RELATED,
                created_at=detected_at,
            )
        )
    return out


# ============================================================================
# CLI factory — build a default SyncOrchestrator wired to real components
# ============================================================================


def build_default_orchestrator(
    *,
    db_path: str | Path,
    token: Optional[str] = None,
    token_pool: Optional["TokenPool"] = None,
    cache_dir: str | Path = Path("data/cache"),
) -> tuple[SyncOrchestrator, SignalRepository]:
    """Construct a fully-wired orchestrator using real adapter + SQLite DB.

    Multi-token support: if ``token_pool`` is provided it is forwarded to the
    adapter directly. Otherwise, if the env var ``GITHUB_TOKENS`` is set
    (comma-separated PATs), a :class:`TokenPool` is built automatically.
    Falls back to single-token behaviour (backward compatible).

    Returns (orchestrator, repository). Caller owns lifecycle — must close
    the repository (and via it the adapter) when done.
    """
    import os

    from src.ingestion.rate_limiter import GitHubRateLimiter, TokenPool

    if token_pool is None:
        entries: list[dict[str, str]] = []

        env_tokens = os.getenv("GITHUB_TOKENS", "").strip()
        if env_tokens:
            entries.extend(
                {"token": t.strip(), "label": f"pat_{i}"}
                for i, t in enumerate(env_tokens.split(","), 1)
                if t.strip()
            )

        for idx in range(1, 21):
            app_id = os.getenv(f"GITHUB_APP_{idx}_ID", "").strip()
            inst_id = os.getenv(f"GITHUB_APP_{idx}_INSTALLATION_ID", "").strip()
            pem_path = os.getenv(f"GITHUB_APP_{idx}_PEM_PATH", "").strip()
            if not (app_id and inst_id and pem_path):
                continue
            pem_file = Path(pem_path).expanduser()
            if not pem_file.exists():
                import logging
                logging.getLogger(__name__).warning(
                    "GITHUB_APP_%s_PEM_PATH %s not found, skipping", idx, pem_file,
                )
                continue
            entries.append({
                "app_id": app_id,
                "installation_id": inst_id,
                "private_key": pem_file.read_text(),
                "label": f"app_{idx}",
            })

        if entries:
            token_pool = TokenPool(entries)

    pool_mode = token_pool is not None and token_pool.pool_size > 1
    repository = SignalRepository(db_path=db_path)
    rate_limiter = GitHubRateLimiter(pool_mode=pool_mode)
    adapter = GitHubAdapter(
        token=token,
        token_pool=token_pool,
        rate_limiter=rate_limiter,
    )
    normalizer = Normalizer()
    change_detector = ChangeDetector()
    cache = JSONCache(cache_dir)

    orchestrator = SyncOrchestrator(
        adapter=adapter,
        normalizer=normalizer,
        change_detector=change_detector,
        repository=repository,
        cache=cache,
    )
    return orchestrator, repository
