"""GitHub REST adapter — httpx direct calls to GitHub v3 REST API.

Implements ``SourceAdapter`` for GitHub issues and pull requests, covering the
three sync modes described in D4 (incremental update pipeline):

* **incremental** → :meth:`GitHubAdapter.discover` with ``since=...``
  (GET ``/repos/{o}/{r}/issues?since=...&state=all&labels=...``)
* **full** → :meth:`GitHubAdapter.discover` without ``since``
* **targeted** → :meth:`GitHubAdapter.fetch_detail`
  (GET ``/repos/{o}/{r}/issues/{n}`` + optional ``/pulls/{n}`` for PR extras)

Comments come from :meth:`GitHubAdapter.fetch_comments` (D4 Step 6), discovery
scans from :meth:`GitHubAdapter.search_issues` (Appendix A, search bucket, 30/min),
and conditional requests from :meth:`GitHubAdapter.check_etag` (Appendix A, ETag).

Design constraints (from the task spec):

* **No body truncation** — ``RawSignal.raw_data`` preserves the GitHub JSON
  as-is so ``Normalizer`` can decide what to keep (D2.1: "preserve full body, no truncation").
* **No raw_data trimming** in the adapter layer; noise compression is the
  Normalizer's job.
* Every request goes through a shared :class:`GitHubRateLimiter` (buckets
  ``"rest"`` and ``"search"``) and is retried up to 3 times on transient
  failures (403 rate-limit, 5xx, network errors) with exponential backoff
  (D4 failure recovery, Scenario C).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from dotenv import load_dotenv

from ..models import RawComment, RawSignal, SourceType

load_dotenv()

logger = logging.getLogger(__name__)


from ..rate_limiter import GitHubRateLimiter, TokenPool
from .base import SourceAdapter, SourceConfig


# ============================================================================
# Module constants
# ============================================================================

_MAX_RETRIES = 3
_DEFAULT_PER_PAGE = 100
_SEARCH_MAX_RESULTS = 1000  # GitHub Search API hard limit (Appendix A)
_ACCEPT_HEADER = "application/vnd.github+json"


# ============================================================================
# Adapter
# ============================================================================


class GitHubAdapter(SourceAdapter):
    """GitHub REST adapter.

    See D4 (incremental update pipeline, Step 3 + Step 6).
    GitHub API call strategy: REST 5000/hr per token, Search 30/min global, ETag conditional requests.

    Usage::

        async with GitHubAdapter() as adapter:
            async for raw in adapter.discover(config, since="2026-04-01T00:00:00Z"):
                ...
            detail = await adapter.fetch_detail("39303", repo="vllm-project/vllm")
            comments = await adapter.fetch_comments("39303", repo="vllm-project/vllm")
    """

    source_type: SourceType = SourceType.GITHUB_ISSUE

    def __init__(
        self,
        token: str | None = None,
        *,
        token_pool: TokenPool | None = None,
        rate_limiter: GitHubRateLimiter | None = None,
        api_base: str = "https://api.github.com",
        api_version: str = "2022-11-28",
        timeout: float = 60.0,
    ) -> None:
        """Construct adapter.

        Args:
            token: GitHub PAT. If ``None``, resolved from env vars
                ``GITHUB_PERSONAL_ACCESS_TOKEN`` (primary) then
                ``GITHUB_TOKEN`` (fallback). If still ``None``, requests run
                unauthenticated (60 req/hr per IP).
            token_pool: Optional :class:`TokenPool` for multi-token rotation.
                When provided, each request dynamically picks the best token.
                Mutually exclusive with ``token`` in intent, but for backward
                compat both may coexist (pool takes precedence).
            rate_limiter: Shared :class:`GitHubRateLimiter`. Creates a new one
                if omitted.
            api_base: GitHub API root. Override for GitHub Enterprise.
            api_version: Value for ``X-GitHub-Api-Version`` header.
            timeout: httpx timeout (seconds).
        """
        resolved_token = (
            token
            or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            or os.getenv("GITHUB_TOKEN")
        )

        headers: dict[str, str] = {
            "Accept": _ACCEPT_HEADER,
            "X-GitHub-Api-Version": api_version,
        }
        if not token_pool and resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        self._token: str | None = resolved_token
        self._token_pool: TokenPool | None = token_pool
        self._api_base = api_base
        self._client = httpx.AsyncClient(
            base_url=api_base,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        self._rate_limiter = rate_limiter or GitHubRateLimiter()

    # ─────────────────────────── lifecycle ────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubAdapter":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @property
    def rate_limiter(self) -> GitHubRateLimiter:
        """Expose the shared rate limiter (read-only access)."""
        return self._rate_limiter

    # ─────────────────────────── helpers ──────────────────────────────────

    @staticmethod
    def _parse_repo(repo: str) -> tuple[str, str]:
        """Split ``owner/name`` into a tuple; raise ``ValueError`` on bad input."""
        parts = (repo or "").strip().split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid repo slug: {repo!r} (expected 'owner/name')")
        return parts[0], parts[1]

    @staticmethod
    def _parse_link_header(link_header: str | None) -> dict[str, str]:
        """Parse an RFC 5988 ``Link`` header into ``{rel: url}``.

        Example input::

            '<https://api.github.com/...>; rel="next", <...>; rel="last"'
        """
        if not link_header:
            return {}
        out: dict[str, str] = {}
        for part in link_header.split(","):
            segments = [seg.strip() for seg in part.split(";") if seg.strip()]
            if len(segments) < 2:
                continue
            url_seg = segments[0]
            if not (url_seg.startswith("<") and url_seg.endswith(">")):
                continue
            url = url_seg[1:-1]
            for seg in segments[1:]:
                if seg.startswith("rel="):
                    rel = seg[4:].strip('"')
                    out[rel] = url
                    break
        return out

    @staticmethod
    def _is_rate_limit_403(resp: httpx.Response) -> bool:
        """Distinguish a rate-limit 403 from a permission 403 (e.g. SAML)."""
        if resp.status_code != 403:
            return False
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        try:
            body_lower = (resp.text or "").lower()
        except Exception:
            return False
        return "rate limit" in body_lower or "api rate limit" in body_lower

    @staticmethod
    def _detect_source_type(item: dict[str, Any]) -> SourceType:
        """Return GITHUB_PR when the payload has a ``pull_request`` key, else
        GITHUB_ISSUE. The ``/issues`` endpoint returns both together.
        """
        return SourceType.GITHUB_PR if "pull_request" in item else SourceType.GITHUB_ISSUE

    def _make_raw_signal(self, item: dict[str, Any]) -> RawSignal:
        """Wrap a raw GitHub JSON item into a ``RawSignal`` *without* trimming.

        ``raw_data`` is the exact API response dict so the Normalizer owns all
        trim/noise decisions (see D2.1: "noise compression is the Normalizer's job").
        """
        number = item.get("number")
        if number is None:
            raise ValueError(
                f"GitHub payload missing 'number' field: keys={list(item.keys())}"
            )
        return RawSignal(
            raw_id=str(number),
            source_type=self._detect_source_type(item),
            raw_data=item,
        )

    @staticmethod
    def _make_raw_comment(row: dict[str, Any]) -> RawComment:
        """Wrap a raw GitHub comment JSON into a ``RawComment`` (no trimming)."""
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        return RawComment(
            comment_id=str(row.get("id", "")),
            author=(user or {}).get("login"),
            body=row.get("body") or "",
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at"),
            raw_data=row,
        )

    # ─────────────────────────── core HTTP ────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        bucket: str = "rest",
    ) -> httpx.Response:
        """Execute a single HTTP request with retries.

        Retry policy (see D4 failure recovery, Scenario C):

        * **403 rate-limit** → sleep until ``x-ratelimit-reset`` + 1s, retry
          up to 3 times.
        * **5xx** / network errors → exponential backoff 1s, 2s, 4s; up to
          3 retries.
        * **Other 4xx** → returned as-is (caller decides: 404/422 are
          legitimate terminal states for some code paths).

        Args:
            method: HTTP method (``"GET"``, ``"HEAD"``, …).
            url: Relative path (``"/repos/..."``) or absolute URL (used for
                following ``Link: rel="next"``).
            params: Query parameters.
            extra_headers: Additional request headers (merged with client
                default headers by httpx).
            bucket: Rate-limit bucket (``"rest"`` or ``"search"``).

        Returns:
            ``httpx.Response``. May still be non-2xx — caller checks.

        Raises:
            ``httpx.HTTPError``: unrecoverable network error after 3 retries.
        """
        backoff = 1.0
        last_error: httpx.HTTPError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                await self._rate_limiter.acquire(bucket)

                req_headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
                current_token: str | None = None
                if self._token_pool:
                    current_token = self._token_pool.get_best_token()
                    if current_token is None:
                        raise RuntimeError(
                            "TokenPool: all tokens unavailable (mint failed or exhausted)"
                        )
                    req_headers["Authorization"] = f"Bearer {current_token}"

                resp = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=req_headers or None,
                )
                self._rate_limiter.update_from_headers(bucket, resp.headers)

                if self._token_pool and current_token and bucket != "search":
                    remaining_str = resp.headers.get("x-ratelimit-remaining")
                    reset_str = resp.headers.get("x-ratelimit-reset")
                    if remaining_str is not None and reset_str is not None:
                        try:
                            self._token_pool.update_token_state(
                                current_token, int(remaining_str), int(reset_str)
                            )
                        except (TypeError, ValueError):
                            pass

                if resp.status_code == 401 and self._token_pool and current_token:
                    self._token_pool.mark_token_unauthorized(current_token)
                    if attempt >= _MAX_RETRIES:
                        return resp
                    logger.warning(
                        "GitHub 401 on %s (token expired?), refreshing (%s/%s)",
                        url, attempt + 1, _MAX_RETRIES,
                    )
                    continue  # immediate retry with refreshed token

                if self._is_rate_limit_403(resp):
                    if self._token_pool and current_token and bucket != "search":
                        self._token_pool.update_token_state(current_token, 0, 0)
                    if attempt >= _MAX_RETRIES:
                        logger.warning(
                            "GitHub rate-limited on %s after %s retries",
                            url,
                            _MAX_RETRIES,
                        )
                        return resp
                    if self._token_pool and bucket != "search":
                        next_token = self._token_pool.get_best_token()
                        if next_token is not None and next_token != current_token:
                            logger.info(
                                "GitHub 403 on %s, rotating to next token (%s/%s)",
                                url,
                                attempt + 1,
                                _MAX_RETRIES,
                            )
                            continue
                    reset_ts = resp.headers.get("x-ratelimit-reset") or "0"
                    try:
                        wait = max(backoff, max(0, int(reset_ts) - int(time.time())) + 1)
                    except ValueError:
                        wait = backoff
                    logger.warning(
                        "GitHub 403 rate-limit on %s, retry in %.1fs (%s/%s)",
                        url,
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue

                if 500 <= resp.status_code < 600:
                    if attempt >= _MAX_RETRIES:
                        return resp
                    logger.warning(
                        "GitHub %s on %s, retry in %.1fs (%s/%s)",
                        resp.status_code,
                        url,
                        backoff,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

                return resp

            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= _MAX_RETRIES:
                    logger.error(
                        "GitHub network error on %s after %s retries: %s",
                        url,
                        _MAX_RETRIES,
                        exc,
                    )
                    raise
                logger.warning(
                    "GitHub network error on %s, retry in %.1fs (%s/%s): %s",
                    url,
                    backoff,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff *= 2

        # Unreachable: the loop either returns or raises.
        assert last_error is not None
        raise last_error

    # ─────────────────── SourceAdapter abstract methods ───────────────────

    async def discover(
        self,
        config: SourceConfig,
        *,
        since: str | None = None,
    ) -> AsyncIterator[RawSignal]:
        """List issues (and PRs — GitHub returns them together).

        Implements D4 Step 3a (incremental/full list). Paginates via
        ``?page=N&per_page=100`` until an empty page or ``max_pages`` is
        reached.

        ``config.params`` expected keys:

        * ``repo``: ``str`` (required, ``"owner/name"``)
        * ``labels``: ``list[str]`` (optional) — AND-filter on GitHub labels
        * ``state``: ``"open" | "closed" | "all"`` (default ``"all"``)
        * ``per_page``: ``int`` (default 100, capped at 100)
        * ``max_pages``: ``int | None`` (default ``None`` — all pages)

        Args:
            config: Source config; see above.
            since: ISO 8601 timestamp. Passed to the GitHub API as
                ``?since=...``, filtering by ``updated_at``.

        Yields:
            ``RawSignal`` for each issue/PR.

        Notes:
            * 404 → logs a warning and returns (no exception).
            * This is a pure list call; PR-specific extras (``merged_by``,
              ``changed_files``) are NOT fetched here to avoid N× extra
              requests. Use :meth:`fetch_detail` for per-PR enrichment.
        """
        params_in = config.params if isinstance(config.params, dict) else {}
        repo = params_in.get("repo")
        if not repo:
            raise ValueError("SourceConfig.params must include 'repo'")
        owner, name = self._parse_repo(repo)

        labels = params_in.get("labels") or []
        state = params_in.get("state", "all")
        if state not in ("open", "closed", "all"):
            raise ValueError(
                f"Invalid state={state!r}; expected 'open'|'closed'|'all'"
            )
        per_page = max(1, min(int(params_in.get("per_page", _DEFAULT_PER_PAGE)), 100))
        max_pages = params_in.get("max_pages")

        query: dict[str, Any] = {
            "state": state,
            "per_page": per_page,
            "page": 1,
            "sort": "updated",
            "direction": "asc" if since else "desc",
        }
        if labels:
            query["labels"] = ",".join(labels) if isinstance(labels, list) else str(labels)
        if since:
            query["since"] = since

        path = f"/repos/{owner}/{name}/issues"
        page = 1
        while True:
            query["page"] = page
            resp = await self._request("GET", path, params=query)

            if resp.status_code == 404:
                logger.warning("discover: 404 for %s (repo missing or private)", repo)
                return
            if resp.status_code != 200:
                resp.raise_for_status()

            try:
                items = resp.json()
            except ValueError as exc:
                logger.warning("discover: invalid JSON on %s page %s: %s", repo, page, exc)
                return

            if not isinstance(items, list) or not items:
                return

            for item in items:
                if isinstance(item, dict):
                    yield self._make_raw_signal(item)

            if len(items) < per_page:
                return
            page += 1
            if max_pages is not None and page > int(max_pages):
                return

    async def fetch_detail(
        self,
        raw_id: str,
        *,
        repo: str,
        **kwargs: Any,
    ) -> RawSignal | None:
        """Fetch one issue or PR by number.

        Implements D4 Step 3 targeted mode. For PRs, additionally fetches
        ``/pulls/{n}`` so ``merged``, ``merged_at``, ``merged_by``,
        ``changed_files`` become available to the Normalizer.

        The extra PR payload is merged into ``raw_data`` under two forms:

        * **Top-level merge**: PR-only keys (``merged``, ``merged_at``,
          ``merged_by``, ``changed_files``, ``additions``, ``deletions``,
          ``mergeable_state``) are copied onto the issue dict for easy access.
        * **Under ``"_pr_detail"``**: the full PR JSON is preserved for any
          downstream consumer that wants the whole thing.

        Args:
            raw_id: Issue/PR number as string.
            repo: ``"owner/name"`` slug.
            **kwargs: Ignored (kept for ``SourceAdapter`` compatibility).

        Returns:
            ``RawSignal`` on success, ``None`` on 404.
        """
        owner, name = self._parse_repo(repo)
        try:
            number = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"fetch_detail: raw_id must be int-convertible, got {raw_id!r}") from exc

        resp = await self._request("GET", f"/repos/{owner}/{name}/issues/{number}")
        if resp.status_code == 404:
            logger.warning("fetch_detail: 404 for %s#%s", repo, number)
            return None
        if resp.status_code != 200:
            resp.raise_for_status()
        issue = resp.json()

        if isinstance(issue, dict) and "pull_request" in issue:
            pr_resp = await self._request("GET", f"/repos/{owner}/{name}/pulls/{number}")
            if pr_resp.status_code == 200:
                try:
                    pr_detail = pr_resp.json()
                except ValueError as exc:
                    logger.warning(
                        "fetch_detail: PR %s#%s returned non-JSON body: %s",
                        repo,
                        number,
                        exc,
                    )
                    pr_detail = None
                if isinstance(pr_detail, dict):
                    issue["_pr_detail"] = pr_detail
                    for key in (
                        "merged",
                        "merged_at",
                        "merged_by",
                        "merge_commit_sha",
                        "changed_files",
                        "additions",
                        "deletions",
                        "commits",
                        "mergeable",
                        "mergeable_state",
                        "draft",
                        "base",
                        "head",
                    ):
                        if key in pr_detail and key not in issue:
                            issue[key] = pr_detail[key]
            else:
                logger.warning(
                    "fetch_detail: /pulls/%s returned %s for %s (issue kept without PR extras)",
                    number,
                    pr_resp.status_code,
                    repo,
                )

        return self._make_raw_signal(issue)

    async def fetch_comments(
        self,
        raw_id: str,
        *,
        repo: str,
        since: str | None = None,
        max_comments: int = 100,
    ) -> list[RawComment]:
        """Fetch issue/PR comments.

        ``GET /repos/{o}/{r}/issues/{n}/comments?since=...&per_page=100``.
        Follows the ``Link: rel="next"`` header until exhausted or
        ``max_comments`` is reached (D4 Step 6a).

        Args:
            raw_id: Issue/PR number as string.
            repo: ``"owner/name"`` slug.
            since: ISO 8601; filters by ``updated_at`` (per GitHub docs —
                not ``created_at``).
            max_comments: Cap on results (safety valve; default 100).

        Returns:
            List of ``RawComment``. Empty on 404 or no comments.
        """
        owner, name = self._parse_repo(repo)
        try:
            number = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"fetch_comments: raw_id must be int-convertible, got {raw_id!r}"
            ) from exc

        query: dict[str, Any] = {"per_page": min(100, max_comments)}
        if since:
            query["since"] = since

        url: str = f"/repos/{owner}/{name}/issues/{number}/comments"
        current_params: Mapping[str, Any] | None = query
        results: list[RawComment] = []

        while True:
            resp = await self._request("GET", url, params=current_params)
            if resp.status_code == 404:
                logger.warning("fetch_comments: 404 for %s#%s", repo, number)
                return results
            if resp.status_code != 200:
                resp.raise_for_status()

            try:
                rows = resp.json()
            except ValueError as exc:
                logger.warning(
                    "fetch_comments: invalid JSON for %s#%s: %s", repo, number, exc
                )
                return results

            if not isinstance(rows, list):
                return results
            for row in rows:
                if isinstance(row, dict):
                    results.append(self._make_raw_comment(row))
                if len(results) >= max_comments:
                    return results

            links = self._parse_link_header(resp.headers.get("Link"))
            next_url = links.get("next")
            if not next_url:
                return results
            url = next_url
            current_params = None  # already baked into the absolute URL

    def make_signal_id(self, raw: dict[str, Any]) -> str:
        """Build the deterministic ``signal_id`` for a GitHub payload.

        Format (see D2.1)::

            github:{owner/repo}:{issue|pr}:{number}

        Detects PR presence by the ``pull_request`` key (from ``/issues``) or
        a ``merged`` key (from ``/pulls``). Extracts repo from
        ``repository_url`` → falls back to ``base.repo.full_name`` (PR
        payload) → ``html_url`` parsing.

        Raises:
            ``ValueError``: when repo or number cannot be determined.
        """
        number = raw.get("number")
        if number is None:
            raise ValueError(
                f"make_signal_id: payload missing 'number'; keys={list(raw.keys())}"
            )

        repo: str | None = None

        repo_url = raw.get("repository_url")
        if isinstance(repo_url, str) and "/repos/" in repo_url:
            repo = repo_url.rsplit("/repos/", 1)[-1].strip("/")

        if not repo:
            base = raw.get("base")
            if isinstance(base, dict):
                base_repo = base.get("repo")
                if isinstance(base_repo, dict):
                    full = base_repo.get("full_name")
                    if isinstance(full, str) and full:
                        repo = full

        if not repo:
            html = raw.get("html_url")
            if isinstance(html, str) and "github.com/" in html:
                tail = html.split("github.com/", 1)[1]
                parts = tail.split("/")
                if len(parts) >= 2:
                    repo = f"{parts[0]}/{parts[1]}"

        if not repo:
            raise ValueError(
                f"make_signal_id: cannot determine repo; keys={list(raw.keys())}"
            )

        is_pr = ("pull_request" in raw) or ("merged" in raw) or ("merged_at" in raw and "issue_url" in raw)
        kind = "pr" if is_pr else "issue"
        return f"github:{repo}:{kind}:{number}"

    # ────────────────────── GitHub-specific extras ────────────────────────

    async def search_issues(
        self,
        query: str,
        *,
        sort: str = "updated",
        order: str = "desc",
        per_page: int = 100,
        max_results: int = 1000,
    ) -> AsyncIterator[RawSignal]:
        """Discovery-mode search (``GET /search/issues``).

        Used for D4 Step 3b and D3.4 discovery mode. The search bucket has a
        30-requests-per-minute limit (Appendix A) and GitHub hard-caps results at
        1000 regardless of pagination.

        Args:
            query: Full GitHub search syntax, e.g.
                ``"repo:vllm-project/vllm label:rocm state:open updated:>=2026-04-01"``.
            sort: ``"updated"``, ``"created"``, ``"comments"``, ``"reactions"``.
            order: ``"asc"`` or ``"desc"``.
            per_page: Page size (capped at 100).
            max_results: Cap on total results (capped at 1000 by GitHub).

        Yields:
            ``RawSignal`` per search hit.

        Notes:
            * 422 → logs a warning and stops iteration (private-repo search
              restriction — Appendix A: "ROCm/aiter is a private repo, search API returns 422").
            * Uses the ``"search"`` rate-limit bucket.
        """
        per_page = max(1, min(int(per_page), 100))
        max_results = max(1, min(int(max_results), _SEARCH_MAX_RESULTS))
        page = 1
        yielded = 0
        while yielded < max_results:
            params: dict[str, Any] = {
                "q": query,
                "sort": sort,
                "order": order,
                "per_page": min(per_page, max_results - yielded),
                "page": page,
            }
            resp = await self._request(
                "GET", "/search/issues", params=params, bucket="search"
            )
            if resp.status_code == 422:
                logger.warning(
                    "search_issues: 422 (often private-repo restriction) query=%r",
                    query,
                )
                return
            if resp.status_code == 404:
                logger.warning("search_issues: 404 query=%r", query)
                return
            if resp.status_code != 200:
                resp.raise_for_status()

            try:
                payload = resp.json()
            except ValueError as exc:
                logger.warning("search_issues: invalid JSON: %s", exc)
                return

            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                return

            for item in items:
                if not isinstance(item, dict):
                    continue
                yield self._make_raw_signal(item)
                yielded += 1
                if yielded >= max_results:
                    return

            if len(items) < params["per_page"]:
                return
            page += 1

    async def fetch_pr_files(
        self,
        raw_id: str,
        *,
        repo: str,
    ) -> list[dict[str, Any]]:
        """Fetch changed files for a PR.

        ``GET /repos/{o}/{r}/pulls/{n}/files`` — returns filenames plus
        inline patches. Paginated via ``?page=N`` with ``per_page=100``.

        Args:
            raw_id: PR number as string.
            repo: ``"owner/name"`` slug.

        Returns:
            Raw list of file objects from the GitHub API (each contains
            ``filename``, ``status``, ``additions``, ``deletions``,
            ``changes``, and ``patch``). Empty on 404.
        """
        owner, name = self._parse_repo(repo)
        try:
            number = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"fetch_pr_files: raw_id must be int-convertible, got {raw_id!r}"
            ) from exc

        path = f"/repos/{owner}/{name}/pulls/{number}/files"
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._request(
                "GET", path, params={"per_page": 100, "page": page}
            )
            if resp.status_code == 404:
                logger.warning("fetch_pr_files: 404 for %s#%s", repo, number)
                return results
            if resp.status_code != 200:
                resp.raise_for_status()

            try:
                rows = resp.json()
            except ValueError as exc:
                logger.warning(
                    "fetch_pr_files: invalid JSON for %s#%s: %s",
                    repo,
                    number,
                    exc,
                )
                return results
            if not isinstance(rows, list) or not rows:
                return results
            results.extend(r for r in rows if isinstance(r, dict))
            if len(rows) < 100:
                return results
            page += 1

    async def check_etag(
        self,
        url: str,
        cached_etag: str | None,
        cached_last_modified: str | None,
    ) -> tuple[int, Any, dict[str, str]]:
        """Conditional request using ETag / Last-Modified.

        See Appendix A "ETag conditional requests". A 304 response does NOT consume REST
        quota on GitHub.

        This method does NOT update the ``etag_cache`` table — that's the
        repository layer's job (D2.2). The caller persists ``new_headers['ETag']``
        and ``new_headers['Last-Modified']`` itself.

        Args:
            url: Relative path (``"/repos/..."``) or absolute URL.
            cached_etag: Previously stored ETag, or ``None``.
            cached_last_modified: Previously stored ``Last-Modified``, or ``None``.

        Returns:
            ``(status_code, body_or_None, new_headers)``:

            * 304 → ``(304, None, headers)`` — caller uses cached data.
            * 200 → ``(200, parsed_json, headers)``.
            * other → ``(status_code, None, headers)``.
        """
        extra: dict[str, str] = {}
        if cached_etag:
            extra["If-None-Match"] = cached_etag
        if cached_last_modified:
            extra["If-Modified-Since"] = cached_last_modified

        resp = await self._request("GET", url, extra_headers=extra or None)
        headers_out = dict(resp.headers)
        if resp.status_code == 304:
            return 304, None, headers_out
        if resp.status_code == 200:
            try:
                return 200, resp.json(), headers_out
            except ValueError as exc:
                logger.warning("check_etag: 200 but invalid JSON on %s: %s", url, exc)
                return 200, None, headers_out
        return resp.status_code, None, headers_out
