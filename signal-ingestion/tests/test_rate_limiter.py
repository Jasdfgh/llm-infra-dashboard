"""Unit tests for ``src.ingestion.rate_limiter.GitHubRateLimiter`` and ``TokenPool``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.ingestion.rate_limiter import GitHubRateLimiter, TokenPool


# ── 1. initial state ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state() -> None:
    rl = GitHubRateLimiter()
    assert rl.rest_remaining == 5000
    assert rl.search_remaining == 30
    assert rl.total_api_calls == 0


# ── 2. acquire("rest") does not block when remaining is plentiful ─────────


@pytest.mark.asyncio
async def test_acquire_rest_no_block() -> None:
    rl = GitHubRateLimiter()
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await rl.acquire("rest")
        mock_sleep.assert_not_called()
    assert rl.total_api_calls == 1


# ── 3. update_from_headers updates remaining ──────────────────────────────


@pytest.mark.asyncio
async def test_update_from_headers() -> None:
    rl = GitHubRateLimiter()
    rl.update_from_headers("rest", {
        "x-ratelimit-remaining": "42",
        "x-ratelimit-reset": "9999999999",
    })
    assert rl.rest_remaining == 42


# ── 4. total_api_calls counts correctly ───────────────────────────────────


@pytest.mark.asyncio
async def test_total_api_calls_counts() -> None:
    rl = GitHubRateLimiter()
    await rl.acquire("rest")
    await rl.acquire("rest")
    await rl.acquire("search")
    assert rl.total_api_calls == 3


# ── 5. acquire("unknown_bucket") → ValueError ────────────────────────────


@pytest.mark.asyncio
async def test_acquire_unknown_bucket() -> None:
    rl = GitHubRateLimiter()
    with pytest.raises(ValueError, match="Unknown bucket"):
        await rl.acquire("graphql")


# ── 6. acquire sleeps when remaining < threshold ──────────────────────────


@pytest.mark.asyncio
async def test_acquire_sleeps_when_remaining_low() -> None:
    rl = GitHubRateLimiter(low_remaining_threshold=10)
    rl.update_from_headers("rest", {
        "x-ratelimit-remaining": "2",
        "x-ratelimit-reset": "9999999999",
    })
    with patch("src.ingestion.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await rl.acquire("rest")
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert slept > 0


# ── 7. header case-insensitive ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_case_insensitive() -> None:
    rl = GitHubRateLimiter()
    rl.update_from_headers("search", {
        "X-RateLimit-Remaining": "7",
        "X-RateLimit-Reset": "9999999999",
    })
    assert rl.search_remaining == 7


# ── 8. concurrent acquire is safe ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_acquire_safe() -> None:
    rl = GitHubRateLimiter()
    await asyncio.gather(
        rl.acquire("rest"),
        rl.acquire("rest"),
        rl.acquire("search"),
    )
    assert rl.total_api_calls == 3


# ── 9. TokenPool: single token always returned ────────────────────────────


def test_token_pool_single_token() -> None:
    pool = TokenPool([{"token": "ghp_aaa", "label": "only"}])
    assert pool.pool_size == 1
    assert pool.get_best_token() == "ghp_aaa"
    assert pool.get_best_token() == "ghp_aaa"


# ── 10. TokenPool: picks token with highest remaining ─────────────────────


def test_token_pool_picks_highest_remaining() -> None:
    pool = TokenPool([
        {"token": "ghp_aaa", "label": "a"},
        {"token": "ghp_bbb", "label": "b"},
        {"token": "ghp_ccc", "label": "c"},
    ])
    pool.update_token_state("ghp_aaa", remaining=0, reset=9999999999)
    pool.update_token_state("ghp_bbb", remaining=4000, reset=9999999999)
    pool.update_token_state("ghp_ccc", remaining=2000, reset=9999999999)
    assert pool.get_best_token() == "ghp_bbb"


# ── 11. TokenPool: all exhausted → earliest reset ────────────────────────


def test_token_pool_all_exhausted_returns_earliest_reset() -> None:
    pool = TokenPool([
        {"token": "ghp_aaa", "label": "a"},
        {"token": "ghp_bbb", "label": "b"},
        {"token": "ghp_ccc", "label": "c"},
    ], low_threshold=100)
    pool.update_token_state("ghp_aaa", remaining=5, reset=2000)
    pool.update_token_state("ghp_bbb", remaining=10, reset=1000)
    pool.update_token_state("ghp_ccc", remaining=0, reset=3000)
    assert pool.get_best_token() == "ghp_bbb"


# ── 12. TokenPool: update_token_state ─────────────────────────────────────


def test_token_pool_update_state() -> None:
    pool = TokenPool([{"token": "ghp_aaa", "label": "a"}])
    pool.update_token_state("ghp_aaa", remaining=42, reset=1234567890)
    assert pool.total_remaining == 42
    pool.update_token_state("ghp_nonexistent", remaining=99, reset=0)
    assert pool.total_remaining == 42


# ── 13. TokenPool: total_remaining sums correctly ────────────────────────


def test_token_pool_total_remaining() -> None:
    pool = TokenPool([
        {"token": "ghp_aaa", "label": "a"},
        {"token": "ghp_bbb", "label": "b"},
    ])
    pool.update_token_state("ghp_aaa", remaining=3000, reset=0)
    pool.update_token_state("ghp_bbb", remaining=2000, reset=0)
    assert pool.total_remaining == 5000
    assert pool.pool_size == 2


# ── 14. TokenPool: empty list raises ValueError ──────────────────────────


def test_token_pool_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least 1 token"):
        TokenPool([])
