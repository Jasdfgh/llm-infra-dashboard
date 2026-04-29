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

    def __init__(
        self,
        *,
        low_remaining_threshold: int = 10,
        pool_mode: bool = False,
    ) -> None:
        """Initialize with optimistic defaults.

        Args:
            low_remaining_threshold: if a bucket's ``remaining`` drops
                strictly below this value during ``acquire``, the caller
                sleeps until the bucket's ``reset_at`` (plus a safety
                buffer) before returning. Default ``10`` gives us a
                small cushion before GitHub would 403 us.
            pool_mode: when ``True``, :meth:`acquire` skips the global
                sleep-on-low logic for the ``rest`` bucket.  Rate
                limiting is instead handled by :class:`TokenPool`'s
                per-token remaining tracking + the adapter's 403 retry.
                The ``search`` bucket (shared across all tokens) still
                blocks normally.
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
        self._pool_mode: bool = pool_mode
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
        skip_sleep = self._pool_mode and bucket == "rest"
        async with self._lock:
            state = self._buckets[bucket]
            if not skip_sleep and state.remaining < self._low_threshold:
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
_APP_TOKEN_REFRESH_BUFFER_SECS: float = 300.0  # refresh 5 min before expiry
_MINT_COOLDOWN_SECS: float = 300.0  # back off 5 min after a failed mint


@dataclass
class _TokenState:
    """Per-token rate-limit state for :class:`TokenPool`."""

    token: str
    label: str
    remaining: int = _REST_DEFAULT_LIMIT
    reset_at: float = 0.0
    # GitHub App fields (None for plain PATs)
    app_id: str | None = None
    private_key: str | None = None
    installation_id: str | None = None
    expires_at: float = float("inf")  # PATs never expire
    _mint_failed: bool = False
    _quarantined: bool = False
    _last_mint_attempt: float = 0.0


def _mint_installation_token(
    app_id: str, private_key: str, installation_id: str,
) -> tuple[str, float]:
    """Generate a GitHub App installation access token (sync HTTP call).

    Returns (token_string, expires_at_epoch).
    """
    import httpx as _httpx

    try:
        import jwt as _jwt
    except ImportError as exc:
        raise ImportError(
            "PyJWT + cryptography required for GitHub App tokens: "
            "pip install PyJWT cryptography"
        ) from exc

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    jwt_token = _jwt.encode(payload, private_key, algorithm="RS256")

    resp = _httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data["token"]
    expires_str = data.get("expires_at", "")
    if expires_str:
        from datetime import datetime, timezone
        expires_at = datetime.fromisoformat(
            expires_str.replace("Z", "+00:00")
        ).timestamp()
    else:
        expires_at = time.time() + 3600  # fallback: 1 hour

    return token, expires_at


class TokenPool:
    """Multi-token rotation for GitHub API rate limit management.

    Supports two token types:

    * **PAT entries**: ``{"token": "ghp_xxx", "label": "..."}`` — static,
      never expire.
    * **App entries**: ``{"app_id": "123", "private_key": "...",
      "installation_id": "456", "label": "..."}`` — installation tokens
      are minted on first use and auto-refreshed 5 minutes before expiry.

    Picks the token with most remaining quota. When all tokens are
    exhausted, returns the one whose reset time is soonest.
    """

    def __init__(
        self,
        tokens: list[dict[str, str]],
        *,
        low_threshold: int = _LOW_REMAINING_THRESHOLD_POOL,
    ) -> None:
        if not tokens:
            raise ValueError("TokenPool requires at least 1 token")

        self._states: dict[str, _TokenState] = {}  # key = label
        self._token_to_label: dict[str, str] = {}   # "ghp_xxx" → label
        self._low_threshold = low_threshold

        for i, entry in enumerate(tokens, 1):
            if "app_id" in entry:
                label = entry.get("label", f"app_{i}")
                state = _TokenState(
                    token="",  # will be minted on first get_best_token()
                    label=label,
                    app_id=entry["app_id"],
                    private_key=entry["private_key"],
                    installation_id=entry["installation_id"],
                    expires_at=0.0,  # force immediate mint
                )
                self._states[label] = state
            else:
                tok = entry["token"]
                label = entry.get("label", f"pat_{i}")
                state = _TokenState(token=tok, label=label)
                self._states[label] = state
                self._token_to_label[tok] = label

    def get_best_token(self) -> str | None:
        """Return the token with highest remaining quota.

        App tokens are refreshed automatically when near expiry.
        Returns ``None`` when every token is unavailable (e.g. all App
        tokens failed their initial mint).
        """
        now = time.time()
        for state in self._states.values():
            if state.app_id and state.expires_at < now + _APP_TOKEN_REFRESH_BUFFER_SECS:
                self._refresh_app_token(state)

        available = [s for s in self._states.values()
                     if not (s._mint_failed and s.token == "")
                     and not s._quarantined]
        if not available:
            return None

        above = [s for s in available if s.remaining >= self._low_threshold]
        if above:
            best = max(above, key=lambda s: (s.remaining, -s.reset_at))
            return best.token
        return min(available, key=lambda s: s.reset_at).token

    def update_token_state(self, token: str, remaining: int, reset: int) -> None:
        """Update state for a specific token after an API response."""
        label = self._token_to_label.get(token)
        if label and label in self._states:
            self._states[label].remaining = remaining
            self._states[label].reset_at = float(reset)

    def mark_token_unauthorized(self, token: str) -> None:
        """Mark a token as unauthorized so it gets quarantined.

        App tokens also get ``expires_at`` zeroed to trigger a refresh
        attempt on the next :meth:`get_best_token` call.  PATs are
        permanently quarantined for the lifetime of this pool (a revoked
        PAT cannot be refreshed).
        """
        label = self._token_to_label.get(token)
        if label and label in self._states:
            state = self._states[label]
            state._quarantined = True
            state.remaining = 0
            if state.app_id:
                state.expires_at = 0.0  # force refresh attempt

    def _refresh_app_token(self, state: _TokenState) -> None:
        """Mint a new installation token, update indexes."""
        assert state.app_id and state.private_key and state.installation_id

        now = time.time()
        if state._mint_failed and (now - state._last_mint_attempt) < _MINT_COOLDOWN_SECS:
            return
        state._last_mint_attempt = now

        old_token = state.token
        if old_token in self._token_to_label:
            del self._token_to_label[old_token]

        try:
            new_token, expires_at = _mint_installation_token(
                state.app_id, state.private_key, state.installation_id,
            )
            state.token = new_token
            state.expires_at = expires_at
            state.remaining = _REST_DEFAULT_LIMIT
            state._mint_failed = False
            state._quarantined = False
            state._last_mint_attempt = 0.0
            self._token_to_label[new_token] = state.label
            import logging
            logging.getLogger(__name__).info(
                "Refreshed App token %s (expires %s)",
                state.label,
                time.strftime("%H:%M:%S", time.localtime(expires_at)),
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to refresh App token %s", state.label,
            )
            if old_token and not state._quarantined:
                self._token_to_label[old_token] = state.label
            else:
                state._mint_failed = True
                state.remaining = 0

    @property
    def total_remaining(self) -> int:
        """Sum of remaining across all tokens."""
        return sum(s.remaining for s in self._states.values())

    @property
    def pool_size(self) -> int:
        """Number of tokens in the pool."""
        return len(self._states)

    def __repr__(self) -> str:
        entries = []
        for s in self._states.values():
            tag = "app" if s.app_id else "pat"
            entries.append(f"{s.label}[{tag}]({s.remaining})")
        return f"TokenPool([{', '.join(entries)}])"


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
