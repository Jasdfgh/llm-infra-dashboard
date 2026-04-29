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


# ═══════════════════════════════════════════════════════════════════════════
# Tests 15–26: App token support, reverse index, pool_mode
# ═══════════════════════════════════════════════════════════════════════════

import time

_MINT_PATCH = "src.ingestion.rate_limiter._mint_installation_token"

_APP_ENTRY: dict[str, str] = {
    "app_id": "123",
    "private_key": "fake_key",
    "installation_id": "456",
    "label": "app_1",
}


def _mock_mint(app_id: str, private_key: str, installation_id: str) -> tuple[str, float]:
    """Mock mint returning a predictable token string."""
    return f"ghs_mock_{app_id}", time.time() + 3600


# ── 15. App entry mints on first use ───────────────────────────────────


@patch(_MINT_PATCH, side_effect=_mock_mint)
def test_token_pool_app_entry_mints_on_first_use(mock_mint) -> None:
    """First get_best_token() must mint an installation token for App entries.

    Without the mint-on-first-use logic, the pool would return the empty
    string placeholder that App entries start with.
    """
    pool = TokenPool([_APP_ENTRY.copy()])
    token = pool.get_best_token()
    mock_mint.assert_called_once_with("123", "fake_key", "456")
    assert token == "ghs_mock_123"
    assert token != ""


# ── 16. Mixed PAT + App pool ──────────────────────────────────────────


@patch(_MINT_PATCH, side_effect=_mock_mint)
def test_token_pool_mixed_pat_and_app(mock_mint) -> None:
    """Pool with both PAT and App entries must track both types.

    Verifies pool_size accounts for both and get_best_token returns a
    valid (non-empty) token regardless of which entry is selected.
    """
    pool = TokenPool([
        {"token": "ghp_pat1", "label": "pat_1"},
        _APP_ENTRY.copy(),
    ])
    assert pool.pool_size == 2
    token = pool.get_best_token()
    assert token in ("ghp_pat1", "ghs_mock_123")


# ── 17. App auto-refresh when near expiry ──────────────────────────────


@patch(_MINT_PATCH, side_effect=_mock_mint)
def test_token_pool_app_auto_refresh_when_near_expiry(mock_mint) -> None:
    """Expired App tokens must be re-minted on the next get_best_token().

    If auto-refresh were removed, the pool would serve a stale token whose
    GitHub credentials have already expired, causing 401 errors.
    """
    pool = TokenPool([_APP_ENTRY.copy()])
    pool.get_best_token()  # initial mint
    assert mock_mint.call_count == 1

    pool._states["app_1"].expires_at = time.time() - 1

    mock_mint.reset_mock()
    pool.get_best_token()  # must trigger auto-refresh
    mock_mint.assert_called_once()


# ── 18. App no refresh when not expired ────────────────────────────────


@patch(_MINT_PATCH, side_effect=_mock_mint)
def test_token_pool_app_no_refresh_when_not_expired(mock_mint) -> None:
    """App tokens with plenty of validity left must NOT be re-minted.

    Unnecessary minting wastes HTTP round-trips to GitHub's token
    endpoint and may invalidate the previous token mid-use.
    """
    pool = TokenPool([_APP_ENTRY.copy()])
    pool.get_best_token()  # initial mint sets expires_at to now + 3600
    assert mock_mint.call_count == 1

    mock_mint.reset_mock()
    pool.get_best_token()  # should NOT re-mint (still ~55 min left)
    mock_mint.assert_not_called()


# ── 19. mark_token_unauthorized forces refresh ─────────────────────────


@patch(_MINT_PATCH, side_effect=_mock_mint)
def test_token_pool_mark_unauthorized_forces_refresh(mock_mint) -> None:
    """mark_token_unauthorized must force re-mint on the next call.

    When GitHub returns 401, the adapter marks the token. Without forcing
    expires_at to 0, the pool would keep serving the revoked token until
    its original expiry time.
    """
    pool = TokenPool([_APP_ENTRY.copy()])
    token = pool.get_best_token()  # initial mint
    assert mock_mint.call_count == 1

    pool.mark_token_unauthorized(token)

    pool.get_best_token()  # must re-mint due to forced expiry
    assert mock_mint.call_count == 2


# ── 20. update_token_state via reverse index ───────────────────────────


def test_token_pool_update_state_via_reverse_index() -> None:
    """update_token_state must locate the correct _TokenState via token→label.

    The internal dict is keyed by label, not token string. Without the
    reverse index, update_token_state would silently drop all updates
    and the pool would never learn real remaining counts from headers.
    """
    pool = TokenPool([
        {"token": "ghp_aaa", "label": "a"},
        {"token": "ghp_bbb", "label": "b"},
    ])
    pool.update_token_state("ghp_bbb", remaining=42, reset=0)
    assert pool._states["b"].remaining == 42
    assert pool._states["a"].remaining == 5000  # untouched


# ── 21. update_token_state with unknown token is no-op ─────────────────


def test_token_pool_update_state_unknown_token_is_noop() -> None:
    """Updating a non-existent token must neither raise nor corrupt state.

    During token rotation, in-flight responses may carry stale tokens;
    the pool must tolerate them gracefully.
    """
    pool = TokenPool([{"token": "ghp_aaa", "label": "a"}])
    pool.update_token_state("nonexistent", remaining=99, reset=0)
    assert pool.total_remaining == 5000  # unchanged


# ── 22. App refresh updates reverse index ──────────────────────────────


def test_token_pool_app_refresh_updates_reverse_index() -> None:
    """After refresh, new token must be in the reverse index and old one removed.

    Without updating _token_to_label, update_token_state(new_token, ...)
    silently fails and the pool never learns the refreshed token's quota.
    """
    call_count = {"n": 0}

    def _sequential_mint(app_id, private_key, installation_id):
        call_count["n"] += 1
        return f"ghs_v{call_count['n']}", time.time() + 3600

    with patch(_MINT_PATCH, side_effect=_sequential_mint):
        pool = TokenPool([_APP_ENTRY.copy()])
        old_token = pool.get_best_token()
        assert old_token == "ghs_v1"

        pool._states["app_1"].expires_at = 0.0
        new_token = pool.get_best_token()
        assert new_token == "ghs_v2"

    # New token in reverse index → update works
    pool.update_token_state("ghs_v2", remaining=42, reset=0)
    assert pool._states["app_1"].remaining == 42

    # Old token gone from reverse index → update is no-op
    pool.update_token_state("ghs_v1", remaining=999, reset=0)
    assert pool._states["app_1"].remaining == 42  # still 42, not 999


# ── 23. repr shows type tag ────────────────────────────────────────────


def test_token_pool_repr_shows_type() -> None:
    """repr must include [pat] and [app] tags for operator debugging.

    Without distinct type tags, operators cannot tell at a glance which
    pool entries are PATs vs. App tokens in log output.
    """
    pool = TokenPool([
        {"token": "ghp_aaa", "label": "my_pat"},
        _APP_ENTRY.copy(),
    ])
    r = repr(pool)
    assert "[pat]" in r
    assert "[app]" in r
    assert "my_pat" in r
    assert "app_1" in r


# ── B. pool_mode tests (24–26) ─────────────────────────────────────────


# ── 24. pool_mode skips rest sleep ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pool_mode_skips_rest_sleep() -> None:
    """pool_mode=True must skip the sleep-on-low logic for the rest bucket.

    In multi-token mode, per-token exhaustion is handled by TokenPool
    rotation. A global sleep would stall all coroutines when only one
    token is exhausted.
    """
    rl = GitHubRateLimiter(pool_mode=True)
    rl.update_from_headers("rest", {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "9999999999",
    })
    with patch("src.ingestion.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await rl.acquire("rest")
        mock_sleep.assert_not_called()


# ── 25. pool_mode still sleeps for search ──────────────────────────────


@pytest.mark.asyncio
async def test_pool_mode_still_sleeps_for_search() -> None:
    """pool_mode=True must still sleep for the search bucket.

    GitHub's /search quota is per-user, not per-token. Token rotation
    cannot help, so the global sleep is still required.
    """
    rl = GitHubRateLimiter(pool_mode=True)
    rl.update_from_headers("search", {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "9999999999",
    })
    with patch("src.ingestion.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await rl.acquire("search")
        mock_sleep.assert_called_once()


# ── 26. non-pool mode still sleeps rest ────────────────────────────────


@pytest.mark.asyncio
async def test_non_pool_mode_still_sleeps_rest() -> None:
    """Default (pool_mode=False) must still sleep on low rest remaining.

    Single-token users have no rotation fallback; the limiter must
    throttle to avoid burning through quota and hitting 403.
    """
    rl = GitHubRateLimiter(pool_mode=False)
    rl.update_from_headers("rest", {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "9999999999",
    })
    with patch("src.ingestion.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await rl.acquire("rest")
        mock_sleep.assert_called_once()
