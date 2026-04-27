"""GitHub API rate-limit tracker.

A small, dependency-free token-bucket + response-header tracker that
throttles calls against two GitHub buckets:

- ``rest``: 5000/hr for authenticated clients (60/hr unauth, we assume
  authenticated).
- ``search``: 30/min for authenticated ``/search/*`` endpoints.

GitHub returns the **same** ``X-RateLimit-*`` header family on every
endpoint, so the calling adapter must tell us which bucket the most
recent response belonged to. See appendix A of
``design/module_1_3_architecture.md`` for the rationale.

Usage::

    rl = GitHubRateLimiter()
    await rl.acquire("rest")
    resp = await client.get(url)
    rl.update_from_headers("rest", resp.headers)

Design decisions:

- **asyncio.Lock, not threading.Lock.** We run in a single asyncio
  event loop; the lock's only job is to keep the ``remaining`` /
  ``reset_at`` fields consistent across concurrent coroutines. A
  blocking thread lock would deadlock an event loop.
- **``update_from_headers`` is synchronous**, not async. HTTP response
  headers are available as soon as the response object exists; we
  don't want callers to have to ``await`` just to record a counter.
  Because it has no ``await`` point, the GIL + single-threaded event
  loop guarantee its tiny-field writes are atomic relative to any
  coroutine waking up from ``acquire``.
- **``acquire`` sleeps outside the lock.** Holding a lock across
  ``asyncio.sleep`` would serialize all throttled calls even when
  different buckets are in play. We snapshot under lock, release,
  sleep, then re-enter under lock to commit.
- **No environment, no filesystem, no HTTP.** This is purely a state
  machine — ingestion tests can mock time with asyncio.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass


# GitHub documented limits. Search bucket also has a 10/min unauth tier,
# but we only care about authenticated use.
_REST_DEFAULT_LIMIT: int = 5000
_SEARCH_DEFAULT_LIMIT: int = 30
_REST_WINDOW_SECS: int = 3600
_SEARCH_WINDOW_SECS: int = 60

# Safety buffer added to every ``sleep_until_reset`` delay so we wake up
# *after* GitHub's server-side counter has rolled over, not exactly on it.
_RESET_SAFETY_BUFFER_SECS: float = 1.0


@dataclass
class _BucketState:
    """Mutable per-bucket state."""

    remaining: int
    reset_at: float  # Unix epoch seconds


class GitHubRateLimiter:
    """GitHub API rate-limit tracker / throttle.

    Listens to ``X-RateLimit-*`` response headers and blocks incoming
    calls when the remaining count drops below a threshold. Supports two
    buckets (``rest`` and ``search``) tracked independently.

    Usage::

        rl = GitHubRateLimiter()
        await rl.acquire("rest")        # blocks if remaining low
        resp = await client.get(url)
        rl.update_from_headers("rest", resp.headers)

    The class is safe to share across asyncio coroutines within one
    event loop; it is **not** thread-safe (threading use would require
    a different lock type).
    """

    _VALID_BUCKETS: tuple[str, ...] = ("rest", "search")

    def __init__(self, *, low_remaining_threshold: int = 10) -> None:
        """Initialize with optimistic defaults.

        Args:
            low_remaining_threshold: if a bucket's ``remaining`` drops
                strictly below this value during ``acquire``, the caller
                sleeps until the bucket's ``reset_at`` (plus a safety
                buffer) before returning. Default ``10`` gives us a
                small cushion before GitHub would 403 us.
        """
        now = time.time()
        self._buckets: dict[str, _BucketState] = {
            "rest": _BucketState(
                remaining=_REST_DEFAULT_LIMIT,
                reset_at=now + _REST_WINDOW_SECS,
            ),
            "search": _BucketState(
                remaining=_SEARCH_DEFAULT_LIMIT,
                reset_at=now + _SEARCH_WINDOW_SECS,
            ),
        }
        self._low_threshold: int = low_remaining_threshold
        self._total_api_calls: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    async def acquire(self, bucket: str) -> None:
        """Block until it is safe to issue one request against ``bucket``.

        Algorithm:

        1. Under lock, read the bucket state; if ``remaining`` is below
           ``low_remaining_threshold``, compute how long to sleep
           (``reset_at - now + safety_buffer``).
        2. Release the lock and sleep (other buckets stay unblocked).
        3. Re-acquire the lock and "assume reset": bump ``remaining``
           back to the default and slide ``reset_at`` one window
           forward. The next real response from the server will
           correct any drift via ``update_from_headers``.
        4. Increment ``total_api_calls``.

        Args:
            bucket: ``"rest"`` or ``"search"``.

        Raises:
            ValueError: if ``bucket`` is not a known bucket name.
        """
        self._check_bucket(bucket)

        sleep_for: float = 0.0
        async with self._lock:
            state = self._buckets[bucket]
            if state.remaining < self._low_threshold:
                delta = state.reset_at - time.time()
                sleep_for = max(0.0, delta) + _RESET_SAFETY_BUFFER_SECS

        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
            async with self._lock:
                self._reset_bucket(bucket)

        async with self._lock:
            self._total_api_calls += 1

    def update_from_headers(
        self, bucket: str, headers: Mapping[str, str]
    ) -> None:
        """Update bucket state from an HTTP response's headers.

        Reads ``X-RateLimit-Remaining`` (count) and ``X-RateLimit-Reset``
        (Unix epoch seconds). Both lookups are case-insensitive-ish:
        we try the lowercase form (httpx / aiohttp normalize to
        lowercase) then the canonical casing as a fallback. Missing or
        non-numeric headers are silently ignored so a partial response
        still records what it can.

        Args:
            bucket: ``"rest"`` or ``"search"``.
            headers: HTTP response headers mapping.

        Raises:
            ValueError: if ``bucket`` is not a known bucket name.
        """
        self._check_bucket(bucket)

        remaining_str = _get_header(headers, "x-ratelimit-remaining")
        reset_str = _get_header(headers, "x-ratelimit-reset")

        state = self._buckets[bucket]
        if remaining_str is not None:
            try:
                state.remaining = int(remaining_str)
            except (TypeError, ValueError):
                # Malformed header — keep previous state.
                pass
        if reset_str is not None:
            try:
                state.reset_at = float(int(reset_str))
            except (TypeError, ValueError):
                pass

    # -------------------------------------------------------------------
    # Read-only views (for metrics / logging)
    # -------------------------------------------------------------------

    @property
    def rest_remaining(self) -> int:
        """Current remaining count for the REST bucket."""
        return self._buckets["rest"].remaining

    @property
    def search_remaining(self) -> int:
        """Current remaining count for the Search bucket."""
        return self._buckets["search"].remaining

    @property
    def total_api_calls(self) -> int:
        """Total ``acquire`` calls since construction.

        Intended to populate ``sync_runs.api_calls_used`` at end of a
        sync. Each successful ``acquire`` (whether it blocked or not)
        counts exactly once.
        """
        return self._total_api_calls

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _check_bucket(self, bucket: str) -> None:
        if bucket not in self._VALID_BUCKETS:
            raise ValueError(
                f"Unknown bucket {bucket!r}; expected one of {self._VALID_BUCKETS}"
            )

    def _reset_bucket(self, bucket: str) -> None:
        """Reset bucket to its default-filled state.

        Called after we've slept past a quota reset; the authoritative
        counter (via ``update_from_headers``) will correct this on the
        next real response.
        """
        now = time.time()
        if bucket == "rest":
            self._buckets["rest"] = _BucketState(
                remaining=_REST_DEFAULT_LIMIT,
                reset_at=now + _REST_WINDOW_SECS,
            )
        else:
            self._buckets["search"] = _BucketState(
                remaining=_SEARCH_DEFAULT_LIMIT,
                reset_at=now + _SEARCH_WINDOW_SECS,
            )


_LOW_REMAINING_THRESHOLD_POOL: int = 100


@dataclass
class _TokenState:
    """Per-token rate-limit state for :class:`TokenPool`."""

    token: str
    label: str
    remaining: int = _REST_DEFAULT_LIMIT
    reset_at: float = 0.0


class TokenPool:
    """Multi-token rotation for GitHub API rate limit management.

    See design doc D6.3. Maintains per-token remaining/reset state.
    Picks the token with most remaining quota. When all tokens are
    exhausted, returns the one whose reset time is soonest (so the
    caller waits the least).

    Backward compatible: single token = pool of 1.
    """

    def __init__(
        self,
        tokens: list[dict[str, str]],
        *,
        low_threshold: int = _LOW_REMAINING_THRESHOLD_POOL,
    ) -> None:
        """
        Args:
            tokens: list of ``{"token": "ghp_xxx", "label": "primary"}`` dicts.
                    At least 1 required.
            low_threshold: When **all** tokens have ``remaining`` below this
                value, :meth:`get_best_token` falls back to earliest-reset
                selection.
        """
        if not tokens:
            raise ValueError("TokenPool requires at least 1 token")
        self._states: dict[str, _TokenState] = {}
        self._order: list[str] = []
        for entry in tokens:
            tok = entry["token"]
            label = entry.get("label", tok[:8])
            self._states[tok] = _TokenState(
                token=tok, label=label, remaining=_REST_DEFAULT_LIMIT
            )
            self._order.append(tok)
        self._low_threshold = low_threshold

    def get_best_token(self) -> str:
        """Return the token with highest remaining quota.

        If all tokens have ``remaining < threshold``, return the one whose
        ``reset_at`` is soonest (so the caller waits the least).
        """
        above = [s for s in self._states.values() if s.remaining >= self._low_threshold]
        if above:
            best = max(above, key=lambda s: (s.remaining, -s.reset_at))
            return best.token
        return min(self._states.values(), key=lambda s: s.reset_at).token

    def update_token_state(self, token: str, remaining: int, reset: int) -> None:
        """Update state for a specific token after an API response."""
        if token in self._states:
            self._states[token].remaining = remaining
            self._states[token].reset_at = float(reset)

    @property
    def total_remaining(self) -> int:
        """Sum of remaining across all tokens."""
        return sum(s.remaining for s in self._states.values())

    @property
    def pool_size(self) -> int:
        """Number of tokens in the pool."""
        return len(self._states)

    def __repr__(self) -> str:
        entries = ", ".join(
            f"{s.label}({s.remaining})" for s in self._states.values()
        )
        return f"TokenPool([{entries}])"


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    httpx.Headers / aiohttp.CIMultiDict are already case-insensitive so
    a direct lookup succeeds; for plain ``dict`` callers we fall back
    to an O(n) scan. Headers maps are tiny (~20 entries) so the cost
    is negligible and the contract stays simple.
    """
    # Fast path for case-insensitive mapping types (httpx/aiohttp).
    try:
        if name in headers:
            return headers[name]
    except (KeyError, TypeError):
        pass

    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None
