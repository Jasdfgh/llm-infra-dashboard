"""Unit tests for ``src.ingestion.adapters.github_adapter.GitHubAdapter``.

Covers 23 cases across make_signal_id, discover, fetch_detail, fetch_comments,
search_issues, retry / error handling, and async context-manager lifecycle.

Mock strategy
~~~~~~~~~~~~~
*  ``adapter._client.request`` (i.e. ``httpx.AsyncClient.request``) is replaced
   by ``unittest.mock.AsyncMock`` — every test constructs real ``httpx.Response``
   objects so ``resp.json()``, ``resp.text``, and ``resp.raise_for_status()``
   behave identically to production.
*  The real ``GitHubRateLimiter`` runs (it is a lightweight in-memory state
   machine with no I/O).
*  ``asyncio.sleep`` is patched only in retry-path tests so they complete
   instantly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from src.ingestion.adapters.base import SourceConfig
from src.ingestion.adapters.github_adapter import GitHubAdapter
from src.ingestion.models import RawComment, RawSignal, SourceType

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — fake HTTP responses & GitHub JSON payloads
# ---------------------------------------------------------------------------


def _resp(
    status_code: int = 200,
    *,
    json_data: Any = None,
    headers: dict[str, str] | None = None,
    text: str | None = None,
) -> httpx.Response:
    """Build a real ``httpx.Response`` with sane rate-limit defaults."""
    h: dict[str, str] = {
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": "9999999999",
    }
    if headers:
        h.update(headers)
    kw: dict[str, Any] = {
        "status_code": status_code,
        "headers": h,
        "request": httpx.Request("GET", "https://api.github.com/test"),
    }
    if json_data is not None:
        kw["json"] = json_data
    elif text is not None:
        kw["text"] = text
    else:
        kw["json"] = {}
    return httpx.Response(**kw)


def _issue(number: int, **overrides: Any) -> dict[str, Any]:
    """Minimal GitHub issue payload."""
    d: dict[str, Any] = {
        "number": number,
        "title": f"Issue #{number}",
        "body": f"Body of #{number}",
        "state": "open",
        "user": {"login": "alice"},
        "labels": [],
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-02T00:00:00Z",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "repository_url": "https://api.github.com/repos/owner/repo",
        "comments": 0,
    }
    d.update(overrides)
    return d


def _pr(number: int, **overrides: Any) -> dict[str, Any]:
    """Minimal GitHub PR payload (as returned by the ``/issues`` endpoint)."""
    d = _issue(number, **overrides)
    d["pull_request"] = {
        "url": f"https://api.github.com/repos/owner/repo/pulls/{number}",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
    }
    d["html_url"] = f"https://github.com/owner/repo/pull/{number}"
    return d


def _comment_json(
    cid: int, author: str = "bob", body: str = "LGTM"
) -> dict[str, Any]:
    """Minimal GitHub comment payload."""
    return {
        "id": cid,
        "user": {"login": author},
        "body": body,
        "created_at": "2026-04-03T10:00:00Z",
        "updated_at": "2026-04-03T11:00:00Z",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def adapter():
    """Fresh ``GitHubAdapter`` with a fake token and real rate limiter."""
    async with GitHubAdapter(token="ghp_fake_for_test") as a:
        yield a


@pytest.fixture
def mock_request(adapter: GitHubAdapter) -> AsyncMock:
    """Replace the underlying ``httpx.AsyncClient.request`` with an AsyncMock."""
    m = AsyncMock()
    adapter._client.request = m
    return m


def _cfg(repo: str = "owner/repo", **extra: Any) -> SourceConfig:
    return SourceConfig(
        source_type=SourceType.GITHUB_ISSUE,
        params={"repo": repo, **extra},
    )


# ===================================================================
# 1–4  make_signal_id (pure function — no HTTP mock)
# ===================================================================


class TestMakeSignalId:
    async def test_issue_with_repository_url(self, adapter: GitHubAdapter):
        """Case 1: issue identified via ``repository_url``."""
        assert adapter.make_signal_id(_issue(42)) == "github:owner/repo:issue:42"

    async def test_pr_with_pull_request_key(self, adapter: GitHubAdapter):
        """Case 2: PR identified by ``pull_request`` key."""
        assert adapter.make_signal_id(_pr(100)) == "github:owner/repo:pr:100"

    async def test_html_url_fallback(self, adapter: GitHubAdapter):
        """Case 3: repo extracted from ``html_url`` when ``repository_url`` absent."""
        raw = {"number": 7, "html_url": "https://github.com/org/proj/issues/7"}
        assert adapter.make_signal_id(raw) == "github:org/proj:issue:7"

    async def test_missing_number_raises(self, adapter: GitHubAdapter):
        """Case 4: missing ``number`` → ``ValueError``."""
        with pytest.raises(ValueError, match="missing 'number'"):
            adapter.make_signal_id({"title": "oops"})


# ===================================================================
# 5–9  discover (async generator + HTTP mock)
# ===================================================================


class TestDiscover:
    async def test_single_page_3_items(self, adapter, mock_request):
        """Case 5: 3 items on one page → yield 3 RawSignal."""
        mock_request.return_value = _resp(
            json_data=[_issue(1), _issue(2), _pr(3)]
        )
        sigs = [s async for s in adapter.discover(_cfg())]
        assert len(sigs) == 3
        assert all(isinstance(s, RawSignal) for s in sigs)
        assert sigs[0].raw_id == "1"
        assert sigs[2].source_type == SourceType.GITHUB_PR

    async def test_two_pages_pagination(self, adapter, mock_request):
        """Case 6: first page full (100), second page partial (20) → 120 total."""
        page1 = [_issue(i) for i in range(100)]
        page2 = [_issue(100 + i) for i in range(20)]
        mock_request.side_effect = [
            _resp(json_data=page1),
            _resp(json_data=page2),
        ]
        sigs = [s async for s in adapter.discover(_cfg())]
        assert len(sigs) == 120

    async def test_empty_page_yields_nothing(self, adapter, mock_request):
        """Case 7: empty first page → yields 0."""
        mock_request.return_value = _resp(json_data=[])
        sigs = [s async for s in adapter.discover(_cfg())]
        assert sigs == []

    async def test_since_forwarded(self, adapter, mock_request):
        """Case 8: ``since`` appears in query params; ``direction`` flips to asc."""
        mock_request.return_value = _resp(json_data=[])
        _ = [
            s
            async for s in adapter.discover(
                _cfg(), since="2026-04-01T00:00:00Z"
            )
        ]
        params = mock_request.call_args.kwargs["params"]
        assert params["since"] == "2026-04-01T00:00:00Z"
        assert params["direction"] == "asc"

    async def test_labels_forwarded(self, adapter, mock_request):
        """Case 9: ``labels`` joined with comma."""
        mock_request.return_value = _resp(json_data=[])
        _ = [s async for s in adapter.discover(_cfg(labels=["bug", "rocm"]))]
        params = mock_request.call_args.kwargs["params"]
        assert params["labels"] == "bug,rocm"


# ===================================================================
# 10–13  fetch_detail (HTTP mock)
# ===================================================================


class TestFetchDetail:
    async def test_issue_ok(self, adapter, mock_request):
        """Case 10: normal issue → ``RawSignal``."""
        mock_request.return_value = _resp(json_data=_issue(42))
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert isinstance(sig, RawSignal)
        assert sig.raw_id == "42"
        assert sig.source_type == SourceType.GITHUB_ISSUE

    async def test_pr_extra_pulls_call(self, adapter, mock_request):
        """Case 11: PR triggers an extra ``/pulls/{n}`` call; merged fields promoted."""
        pr_detail = {
            "number": 50,
            "merged": True,
            "merged_at": "2026-04-05T00:00:00Z",
            "merged_by": {"login": "merger"},
            "changed_files": 5,
            "additions": 100,
            "deletions": 20,
            "mergeable_state": "clean",
        }
        mock_request.side_effect = [
            _resp(json_data=_pr(50)),
            _resp(json_data=pr_detail),
        ]
        sig = await adapter.fetch_detail("50", repo="owner/repo")
        assert sig is not None
        assert sig.source_type == SourceType.GITHUB_PR
        assert sig.raw_data["merged"] is True
        assert sig.raw_data["changed_files"] == 5
        assert sig.raw_data["_pr_detail"] == pr_detail
        assert mock_request.call_count == 2

    async def test_404_returns_none(self, adapter, mock_request):
        """Case 12: 404 → ``None``."""
        mock_request.return_value = _resp(404)
        assert await adapter.fetch_detail("999", repo="owner/repo") is None

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_rate_limit_403_retries(
        self, mock_sleep, adapter, mock_request
    ):
        """Case 13: rate-limit 403 triggers retry; second attempt succeeds."""
        rl_resp = _resp(
            403,
            json_data={"message": "API rate limit exceeded"},
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "0",
            },
        )
        mock_request.side_effect = [rl_resp, _resp(json_data=_issue(42))]
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert sig is not None and sig.raw_id == "42"
        assert mock_sleep.await_count >= 1

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_secondary_rate_limit_retries(
        self, mock_sleep, adapter, mock_request
    ):
        """429 secondary rate limit with Retry-After → sleep and retry."""
        resp_429 = _resp(
            429,
            json_data={"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "10"},
        )
        mock_request.side_effect = [resp_429, _resp(json_data=_issue(42))]
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert sig is not None and sig.raw_id == "42"
        assert mock_sleep.await_count >= 1
        # Verify sleep was called with at least 5s (our minimum clamp)
        sleep_arg = mock_sleep.call_args_list[0][0][0]
        assert sleep_arg >= 5.0

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_exhausts_retries(
        self, mock_sleep, adapter, mock_request
    ):
        """429 on all attempts → _request returns 429 resp, fetch_detail raises HTTPStatusError."""
        resp_429 = _resp(
            429,
            json_data={"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "5"},
        )
        mock_request.return_value = resp_429
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.fetch_detail("42", repo="owner/repo")
        assert exc_info.value.response.status_code == 429

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_missing_retry_after_capped(
        self, mock_sleep, adapter, mock_request
    ):
        """429 with no Retry-After header → fallback sleep capped at 300s."""
        resp_429 = _resp(
            429,
            json_data={"message": "secondary rate limit"},
            headers={},
        )
        mock_request.side_effect = [resp_429, resp_429, resp_429, resp_429]
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.fetch_detail("42", repo="owner/repo")
        for call in mock_sleep.call_args_list:
            assert call[0][0] <= 300.0, f"sleep {call[0][0]} exceeds 300s cap"
            assert call[0][0] >= 5.0, f"sleep {call[0][0]} below 5s minimum"

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_non_numeric_retry_after_capped(
        self, mock_sleep, adapter, mock_request
    ):
        """429 with non-numeric Retry-After → fallback sleep capped at 300s."""
        resp_429 = _resp(
            429,
            json_data={"message": "secondary rate limit"},
            headers={"Retry-After": "n/a"},
        )
        mock_request.side_effect = [resp_429, _resp(json_data=_issue(42))]
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert sig is not None
        sleep_val = mock_sleep.call_args_list[0][0][0]
        assert 5.0 <= sleep_val <= 300.0, f"sleep {sleep_val} outside [5, 300]"

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_negative_retry_after(
        self, mock_sleep, adapter, mock_request
    ):
        """429 with negative Retry-After → clamped to minimum 5s."""
        resp_429 = _resp(
            429,
            json_data={"message": "secondary rate limit"},
            headers={"Retry-After": "-10"},
        )
        mock_request.side_effect = [resp_429, _resp(json_data=_issue(42))]
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert sig is not None
        sleep_val = mock_sleep.call_args_list[0][0][0]
        assert sleep_val >= 5.0, f"sleep {sleep_val} below 5s minimum"

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_429_huge_retry_after_capped(
        self, mock_sleep, adapter, mock_request
    ):
        """429 with huge Retry-After (99999) → capped at 300s."""
        resp_429 = _resp(
            429,
            json_data={"message": "secondary rate limit"},
            headers={"Retry-After": "99999"},
        )
        mock_request.side_effect = [resp_429, _resp(json_data=_issue(42))]
        sig = await adapter.fetch_detail("42", repo="owner/repo")
        assert sig is not None
        sleep_val = mock_sleep.call_args_list[0][0][0]
        assert sleep_val == 300.0, f"expected 300s cap, got {sleep_val}"


# ===================================================================
# 14–17  fetch_comments (HTTP mock)
# ===================================================================


class TestFetchComments:
    async def test_3_comments(self, adapter, mock_request):
        """Case 14: 3 comments → 3 ``RawComment`` with correct fields."""
        rows = [
            _comment_json(1, "alice", "First!"),
            _comment_json(2, "bob", "LGTM"),
            _comment_json(3, "charlie", "+1"),
        ]
        mock_request.return_value = _resp(json_data=rows)
        result = await adapter.fetch_comments("42", repo="owner/repo")
        assert len(result) == 3
        assert all(isinstance(c, RawComment) for c in result)
        assert result[0].author == "alice"
        assert result[0].body == "First!"
        assert result[1].comment_id == "2"
        assert result[2].created_at == "2026-04-03T10:00:00Z"

    async def test_empty_comments(self, adapter, mock_request):
        """Case 15: no comments → empty list."""
        mock_request.return_value = _resp(json_data=[])
        assert await adapter.fetch_comments("42", repo="owner/repo") == []

    async def test_bot_author_preserved(self, adapter, mock_request):
        """Case 16: adapter returns bot comments as-is (filtering is normalizer's job)."""
        mock_request.return_value = _resp(
            json_data=[_comment_json(10, "github-actions[bot]", "CI passed")]
        )
        result = await adapter.fetch_comments("42", repo="owner/repo")
        assert len(result) == 1
        assert result[0].author == "github-actions[bot]"

    async def test_since_forwarded(self, adapter, mock_request):
        """Case 17: ``since`` appears in the query params."""
        mock_request.return_value = _resp(json_data=[])
        await adapter.fetch_comments(
            "42", repo="owner/repo", since="2026-04-01T00:00:00Z"
        )
        params = mock_request.call_args.kwargs["params"]
        assert params["since"] == "2026-04-01T00:00:00Z"


# ===================================================================
# 18–20  search_issues (HTTP mock)
# ===================================================================


class TestSearchIssues:
    async def test_search_yields_signals(self, adapter, mock_request):
        """Case 18: Search API ``items`` → yield ``RawSignal``."""
        mock_request.return_value = _resp(
            json_data={"items": [_issue(1), _pr(2)], "total_count": 2}
        )
        sigs = [
            s async for s in adapter.search_issues("repo:owner/repo label:bug")
        ]
        assert len(sigs) == 2
        assert sigs[0].raw_id == "1"
        assert sigs[1].source_type == SourceType.GITHUB_PR

    async def test_422_yields_nothing(self, adapter, mock_request):
        """Case 19: 422 (private-repo restriction) → yield 0, no exception."""
        mock_request.return_value = _resp(
            422, json_data={"message": "Validation Failed"}
        )
        sigs = [s async for s in adapter.search_issues("repo:private/repo")]
        assert sigs == []

    async def test_empty_results(self, adapter, mock_request):
        """Case 20: empty ``items`` → yield 0."""
        mock_request.return_value = _resp(
            json_data={"items": [], "total_count": 0}
        )
        sigs = [s async for s in adapter.search_issues("repo:owner/repo")]
        assert sigs == []


# ===================================================================
# 21–22  error handling
# ===================================================================


class TestErrorHandling:
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_network_error_retries_then_raises(
        self, mock_sleep, adapter, mock_request
    ):
        """Case 21: ``ConnectError`` × 4 (initial + 3 retries) → raises."""
        mock_request.side_effect = httpx.ConnectError("Connection refused")
        with pytest.raises(httpx.ConnectError):
            await adapter.fetch_detail("42", repo="owner/repo")
        assert mock_request.call_count == 4
        assert mock_sleep.await_count == 3

    async def test_non_ratelimit_403_raises(self, adapter, mock_request):
        """Case 22: SAML 403 (remaining > 0, body ≠ 'rate limit') → ``HTTPStatusError``."""
        mock_request.return_value = _resp(
            403,
            json_data={
                "message": "Resource protected by organization SAML enforcement"
            },
            headers={"x-ratelimit-remaining": "4999"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.fetch_detail("42", repo="owner/repo")


# ===================================================================
# 23  context manager lifecycle
# ===================================================================


class TestContextManager:
    async def test_enter_exit(self):
        """Case 23: ``async with`` works and closes the httpx client on exit."""
        async with GitHubAdapter(token="test") as a:
            assert isinstance(a, GitHubAdapter)
        assert a._client.is_closed


# ===================================================================
# 24–25  401 handling with App token refresh
# ===================================================================


class TestUnauthorizedRetry:
    """Tests for 401 handling with App token refresh."""

    @pytest_asyncio.fixture
    async def pool_adapter(self):
        """Adapter wired with a mock TokenPool for 401-retry tests."""
        from unittest.mock import MagicMock

        pool = MagicMock()
        pool.get_best_token = MagicMock(side_effect=["old_token", "new_token"])
        pool.mark_token_unauthorized = MagicMock()
        pool.update_token_state = MagicMock()

        async with GitHubAdapter(token_pool=pool) as a:
            yield a, pool

    @pytest.fixture
    def mock_pool_request(self, pool_adapter) -> tuple[AsyncMock, Any]:
        adapter, pool = pool_adapter
        m = AsyncMock()
        adapter._client.request = m
        return m, pool

    async def test_401_triggers_mark_and_retry(self, pool_adapter, mock_pool_request):
        """401 response should mark token unauthorized and retry immediately."""
        mock_req, pool = mock_pool_request
        mock_req.side_effect = [
            _resp(401, json_data={"message": "Bad credentials"}),
            _resp(200, json_data=_issue(42)),
        ]

        adapter, _ = pool_adapter
        resp = await adapter._request("GET", "/repos/owner/repo/issues/42")

        assert resp.status_code == 200
        pool.mark_token_unauthorized.assert_called_once_with("old_token")
        assert mock_req.call_count == 2

    async def test_401_without_token_pool_returns_response(self, adapter, mock_request):
        """Without token_pool, 401 is just returned (no retry)."""
        mock_request.return_value = _resp(
            401, json_data={"message": "Bad credentials"}
        )
        resp = await adapter._request("GET", "/repos/owner/repo/issues/42")

        assert resp.status_code == 401
        assert mock_request.call_count == 1
