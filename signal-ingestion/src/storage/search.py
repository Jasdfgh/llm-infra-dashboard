"""SignalSearch — FTS5 + indexed multi-filter search over signals.db.

Implements the read-side API on top of ``SignalRepository``:

* ``search()``     — multi-filter query (FTS5 if ``query`` given, else
                     indexed column scan). See 附录 D (组合查询内部路由).
* ``get_detail()`` — full signal + comments, with all JSON fields parsed.
* ``get_feed()``   — D3.1 pull-based incremental feed for Module 2.

Routing summary (附录 D)::

    query non-empty → SELECT s.* FROM signals_fts fts
                      JOIN signals s ON s.rowid = fts.rowid
                      WHERE signals_fts MATCH ? AND <filters…>
    query empty    → SELECT s.* FROM signals s
                      WHERE 1=1 AND <filters…>

    labels=[L]     → EXISTS (SELECT 1 FROM signal_labels WHERE signal_id=s.signal_id AND label=L)
    gap_ids=[G]    → EXISTS (SELECT 1 FROM signal_gap_ids WHERE signal_id=s.signal_id AND gap_id=G)

Pagination for ``get_feed`` is keyset-based (``(last_synced_at,
signal_id)``) — NOT ``OFFSET`` — because OFFSET scales linearly on
large tables (附录 D note at the bottom).

Column-physical filters (``github_state``, ``github_labels``, …) are
used instead of ``json_extract(github_json, '$.state')`` for 10–100x
speedup with the existing indexes (see D2.2 rationale for the
redundant columns).
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
from typing import Any, Literal, Optional

from src.storage.repository import SignalRepository

_FTS_OPS = {"AND", "OR", "NOT"}


def _sanitize_fts_query(raw: str) -> str:
    """Minimal FTS5 sanitization — preserve as much valid syntax as possible.

    **Supported FTS5 syntax** (preserved as-is):

    * ``AND`` / ``OR`` / ``NOT`` operators
    * ``"quoted phrases"``
    * ``(grouping)``
    * ``column:qualifier`` and ``{col1 col2}:term`` (column sets)
    * ``-column:term`` (negative column filter)
    * ``prefix*``
    * ``^term`` (initial-token anchor)
    * ``NEAR(term1 term2, N)`` (proximity)

    **Not preserved** (cleaned to space): ``+`` (conflicts with ``C++``
    which is common in our domain).

    Only fixes input that is clearly broken:

    * Unbalanced quotes → auto-close.
    * ``NEAR/N`` (bare, outside parens) → strip.
    * Characters with no valid FTS5 meaning
      (``[ ] # = / @ $ % & ! ? ~ < > \\ | ; '``) → space.
    * Known qualifier typos (for example ``title::foo`` or
      ``title: :foo``) → normalize to ``title: foo`` as a
      user-friendly tolerance.
    * ``AND NOT`` → ``NOT`` (semantic-preserving normalisation).
    * Degenerate operator-only input → return empty.

    Invalid but structurally intact FTS5 expressions (leading ``NOT``,
    ``OR NOT``, unbalanced parens, …) are passed through so that
    FTS5 itself rejects them — the caller wraps execution in
    ``try/except`` and surfaces the error.

    Returns empty string only for degenerate input (no search terms).
    """
    if not raw or not raw.strip():
        return ""

    text = raw.strip()

    # --- 1. Fix unbalanced quotes -----------------------------------------
    if text.count('"') % 2 != 0:
        text += '"'

    # --- 2-4. Transform ONLY outside quoted phrases -----------------------
    #   Split into quoted / non-quoted segments so that NEAR/N stripping,
    #   AND NOT normalisation, and character cleaning never touch the
    #   literal content of a "phrase query".
    segments = re.split(r'("(?:[^"]*)")', text)
    cleaned: list[str] = []
    for seg in segments:
        if seg.startswith('"') and seg.endswith('"'):
            cleaned.append(seg)  # quoted phrase — pass through
        else:
            seg = re.sub(r"\bNEAR/\d+\b", " ", seg, flags=re.IGNORECASE)
            seg = re.sub(r'[^\w\s*:()"{}^,\-]', " ", seg)

            # Context-sensitive colon: keep only as FTS5 column qualifier.
            # Word-boundary + case-insensitive so TITLE: works but
            # subtitle: doesn't. Extra colons/spaces after a known
            # qualifier are intentionally cleaned away, so `title::foo`
            # becomes `title: foo` instead of surfacing a syntax error.
            _COL_PH = "\x01"
            seg = re.sub(
                r"\b(title|body|tags):",
                lambda m: m.group(1) + _COL_PH,
                seg, flags=re.IGNORECASE,
            )
            seg = seg.replace("}:", "}" + _COL_PH)
            seg = seg.replace(":", " ")
            seg = seg.replace(_COL_PH, ":")

            # Context-sensitive comma: keep only inside NEAR().
            _COMMA_PH = "\x02"
            seg = re.sub(
                r"(NEAR\([^)]*),([^)]*\))",
                lambda m: m.group(1) + _COMMA_PH + m.group(2),
                seg, flags=re.IGNORECASE,
            )
            seg = seg.replace(",", " ")
            seg = seg.replace(_COMMA_PH, ",")

            # Context-sensitive hyphen: keep only before column qualifiers
            # that survived the colon step above.
            seg = re.sub(r"-(?!\w+:|{)", " ", seg)

            seg = re.sub(r"\bAND\s+NOT\b", "NOT", seg, flags=re.IGNORECASE)
            cleaned.append(seg)
    text = "".join(cleaned)

    # --- 5. Normalise whitespace ------------------------------------------
    text = " ".join(text.split())

    if not text:
        return ""

    # --- 6. Check for at least one actual search term ---------------------
    outside_quotes = re.sub(r'"[^"]*"', " ", text)
    has_terms = any(
        tok
        for tok in outside_quotes.split()
        if tok.upper() not in _FTS_OPS and tok not in ("(", ")")
    )
    if not has_terms:
        quoted_content = re.findall(r'"([^"]+)"', text)
        if not any(c.strip() for c in quoted_content):
            return ""

    return text

# Fields projected into the compact search result dict. Mirrors the
# shape documented in the API spec (D3.1 for feed, plus the extra
# fields useful for a human-facing search UI).
_SEARCH_FIELDS: tuple[str, ...] = (
    "signal_id",
    "source_type",
    "source_url",
    "source_repo",
    "source_number",
    "title",
    "author",
    "created_at",
    "updated_at",
    "last_synced_at",
    "version",
    "body_token_estimate",
    "github_state",
    "github_comment_count",
    "github_is_pr",
)

# JSON TEXT columns that the search layer auto-parses on the way out.
_JSON_COLUMNS: tuple[str, ...] = (
    "tags",
    "github_labels",
    "gap_ids",
)

# Detail response also surfaces the heavy JSON blobs, parsed.
_DETAIL_JSON_COLUMNS: tuple[str, ...] = (
    "references_json",
    "github_json",
    "twitter_json",
    "arxiv_json",
    "classification_json",
)

_BODY_PREVIEW_CHARS: int = 200


def _parse_json(value: Any, *, default: Any = None) -> Any:
    """Best-effort JSON-decode. Returns ``default`` on None or error."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_search_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Project a raw signals row into the compact search result shape.

    Parses the JSON-text columns (``tags``, ``github_labels``,
    ``gap_ids``) and truncates ``body`` to a 200-char preview (D3.1).
    Never includes the full ``body`` or the large ``*_json`` blobs —
    use :func:`SignalSearch.get_detail` for that.
    """
    out: dict[str, Any] = {k: row.get(k) for k in _SEARCH_FIELDS}
    for col in _JSON_COLUMNS:
        out[col] = _parse_json(row.get(col), default=[])
    out["github_is_pr"] = bool(row.get("github_is_pr"))
    body: Optional[str] = row.get("body")
    if body is not None:
        out["body_preview"] = body[:_BODY_PREVIEW_CHARS]
    else:
        out["body_preview"] = ""
    return out


def _row_to_detail_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Full signal detail — includes body and every parsed JSON blob."""
    out: dict[str, Any] = dict(row)  # start from raw row
    # Parse list-valued JSON columns.
    for col in _JSON_COLUMNS:
        out[col] = _parse_json(row.get(col), default=[])
    # Parse object-valued JSON blobs.
    for col in _DETAIL_JSON_COLUMNS:
        out[col] = _parse_json(row.get(col), default=None)
    out["github_is_pr"] = bool(row.get("github_is_pr"))
    return out


class SignalSearch:
    """Search over signals.db. Reuses ``SignalRepository``'s connection.

    Usage::

        with SignalRepository(db_path) as repo:
            search = SignalSearch(repo)
            results = search.search(query="aiter MLA", repos=["vllm-project/vllm"])
    """

    def __init__(self, repository: SignalRepository) -> None:
        """Adopt the repository's connection (no new one opened)."""
        self._repo = repository
        self._conn = repository.connection

    # ── search(): multi-filter query ──────────────────────

    def search(
        self,
        *,
        query: str | None = None,
        repos: list[str] | None = None,
        source_types: list[str] | None = None,
        labels: list[str] | None = None,
        state: Literal["open", "closed", "all"] | None = None,
        since: str | None = None,
        until: str | None = None,
        gap_ids: list[str] | None = None,
        sort: Literal["relevance", "updated", "created"] = "updated",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Multi-filter search.

        Args:
            query: FTS5 match expression over ``title``/``body``/``tags``.
                If None/empty, skips FTS5 and does a pure indexed scan.
            repos: filter ``source_repo IN (…)``.
            source_types: filter ``source_type IN (…)``.
            labels: each label must appear in ``github_labels`` JSON.
            state: ``open``/``closed``/``all`` (``all`` disables filter).
            since / until: ``updated_at`` range (inclusive).
            gap_ids: each gap_id must appear in ``gap_ids`` JSON.
            sort: ``relevance`` falls back to ``updated`` if no ``query``.
            limit / offset: paging. Prefer ``get_feed`` for Module 2 —
                OFFSET is slow on large tables (附录 D).

        Returns::

            {
              "results": [ {search_dict}, … ],
              "total":   <int — matching rows before LIMIT/OFFSET>,
              "meta":    { "query_time_ms": <int>,
                           "filters_applied": { … } }
            }
        """
        t0 = time.perf_counter()

        fts_mode = bool(query and query.strip())

        if fts_mode:
            sanitized = _sanitize_fts_query(query)
            if not sanitized:
                # User intended a search but input has no usable terms.
                # Return empty results — do NOT fall through to a full scan.
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                return {
                    "results": [],
                    "total": 0,
                    "meta": {
                        "query_time_ms": elapsed_ms,
                        "filters_applied": {"query": query},
                    },
                }
        else:
            sanitized = ""

        if fts_mode:
            base_select = (
                "SELECT s.* FROM signals_fts fts "
                "JOIN signals s ON s.rowid = fts.rowid"
            )
        else:
            base_select = "SELECT s.* FROM signals s"

        where: list[str] = []
        params: list[Any] = []

        if fts_mode:
            where.append("signals_fts MATCH ?")
            params.append(sanitized)

        if repos:
            placeholders = ",".join("?" * len(repos))
            where.append(f"s.source_repo IN ({placeholders})")
            params.extend(repos)

        if source_types:
            placeholders = ",".join("?" * len(source_types))
            where.append(f"s.source_type IN ({placeholders})")
            params.extend(source_types)

        if labels:
            for lab in labels:
                where.append(
                    "EXISTS (SELECT 1 FROM signal_labels sl "
                    "WHERE sl.signal_id = s.signal_id AND sl.label = ?)"
                )
                params.append(lab)

        if gap_ids:
            for g in gap_ids:
                where.append(
                    "EXISTS (SELECT 1 FROM signal_gap_ids sg "
                    "WHERE sg.signal_id = s.signal_id AND sg.gap_id = ?)"
                )
                params.append(g)

        if state and state != "all":
            where.append("s.github_state = ?")
            params.append(state)

        if since:
            where.append("s.updated_at >= ?")
            params.append(since)

        if until:
            where.append("s.updated_at <= ?")
            params.append(until)

        where_clause = f" WHERE {' AND '.join(where)}" if where else ""
        base_sql = base_select + where_clause

        # COUNT(*) wrapped subquery — correct for both FTS and non-FTS
        # paths, because the subquery preserves DISTINCT rowids.
        count_sql = f"SELECT COUNT(*) AS n FROM ({base_sql})"

        # Sort + pagination. Only ``relevance`` depends on FTS; others
        # use the already-indexed physical columns.
        order_sql = self._order_sql(sort, fts_mode)
        page_sql = f"{base_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        page_params = [*params, limit, offset]

        try:
            total = int(
                self._conn.execute(count_sql, params).fetchone()["n"]
            )
            rows = [
                dict(r)
                for r in self._conn.execute(page_sql, page_params).fetchall()
            ]
        except sqlite3.OperationalError as exc:
            if not fts_mode:
                raise  # non-FTS errors should still propagate
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {
                "results": [],
                "total": 0,
                "meta": {
                    "query_time_ms": elapsed_ms,
                    "error": str(exc),
                    "filters_applied": {
                        "query": query,
                        "sanitized": sanitized,
                    },
                },
            }

        results = [_row_to_search_dict(r) for r in rows]

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "results": results,
            "total": total,
            "meta": {
                "query_time_ms": elapsed_ms,
                "filters_applied": {
                    "query": query,
                    "repos": repos,
                    "source_types": source_types,
                    "labels": labels,
                    "state": state,
                    "since": since,
                    "until": until,
                    "gap_ids": gap_ids,
                    "sort": sort,
                    "limit": limit,
                    "offset": offset,
                },
            },
        }

    @staticmethod
    def _order_sql(sort: str, fts_mode: bool) -> str:
        """Return the ``ORDER BY`` tail for the given sort mode.

        ``relevance`` only makes sense when FTS5 MATCH was used; fall
        back to ``updated_at DESC`` otherwise (附录 D).
        """
        if sort == "created":
            return "s.created_at DESC"
        if sort == "relevance":
            return "fts.rank" if fts_mode else "s.updated_at DESC"
        return "s.updated_at DESC"

    # ── get_detail(): full signal + comments ──────────────

    def get_detail(
        self,
        signal_id: str,
        *,
        include_body: bool = True,
        include_comments: bool = True,
        max_comments: int = 50,
    ) -> Optional[dict[str, Any]]:
        """Fetch full detail by signal_id. Returns None if not found.

        All JSON TEXT columns are parsed into native dict/list values
        (unlike :meth:`SignalRepository.get_by_id`, which returns raw
        strings). Pass ``include_body=False`` to omit the full body
        (keeps a 200-char preview under ``body_preview``).
        """
        row = self._repo.get_by_id(signal_id)
        if row is None:
            return None
        detail = _row_to_detail_dict(row)

        body: Optional[str] = detail.get("body")
        if body is not None:
            detail["body_preview"] = body[:_BODY_PREVIEW_CHARS]
        if not include_body:
            detail.pop("body", None)

        if include_comments:
            detail["comments"] = self._repo.get_comments(
                signal_id, limit=max_comments
            )
        return detail

    # ── get_feed(): D3.1 incremental feed for Module 2 ────

    def get_feed(
        self,
        *,
        since: str,
        classified: bool | None = False,
        source_types: list[str] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """D3.1 pull-based incremental feed.

        Args:
            since: ISO-8601 UTC. Matches ``last_synced_at >= since``.
            classified: False = only unclassified (``classification_json
                IS NULL``, the default for Module 2 consumers).
                True = only classified. None = no filter.
            source_types: restrict to e.g. ``["github_issue", "github_pr"]``.
            limit: page size (capped at 500 per D3.1).
            cursor: base64("last_synced_at|signal_id") from a previous
                call's ``pagination.next_cursor``.

        Returns::

            {
              "signals":    [ … ],
              "pagination": { "total": n,  "returned": n,
                              "next_cursor": str|None, "has_more": bool },
              "meta":       { "query_since": since, "query_time_ms": n }
            }

        Keyset pagination note
        ----------------------
        We sort by ``(last_synced_at ASC, signal_id ASC)`` — the partial
        index ``idx_signals_feed`` backs this perfectly when
        ``classified is False``. The cursor encodes the last row's key,
        and the next page continues with ``(key) > (cursor_key)``.
        OFFSET is avoided per 附录 D.
        """
        t0 = time.perf_counter()
        limit = max(1, min(limit, 500))  # D3.1: max 500

        where: list[str] = ["last_synced_at >= ?"]
        params: list[Any] = [since]

        if classified is False:
            where.append("classification_json IS NULL")
        elif classified is True:
            where.append("classification_json IS NOT NULL")
        # classified is None → no filter

        if source_types:
            placeholders = ",".join("?" * len(source_types))
            where.append(f"source_type IN ({placeholders})")
            params.extend(source_types)

        # Count total matching rows (before cursor/limit).
        count_sql = (
            f"SELECT COUNT(*) AS n FROM signals "
            f"WHERE {' AND '.join(where)}"
        )
        total = int(self._conn.execute(count_sql, params).fetchone()["n"])

        # Apply cursor — keyset pagination on (last_synced_at, signal_id).
        cursor_ts, cursor_sid = self._decode_cursor(cursor)
        if cursor_ts is not None and cursor_sid is not None:
            where.append("(last_synced_at, signal_id) > (?, ?)")
            params.extend([cursor_ts, cursor_sid])

        # Peek one extra row to detect has_more without a second query.
        page_sql = (
            f"SELECT * FROM signals WHERE {' AND '.join(where)} "
            f"ORDER BY last_synced_at ASC, signal_id ASC LIMIT ?"
        )
        page_params = [*params, limit + 1]
        raw_rows = [
            dict(r)
            for r in self._conn.execute(page_sql, page_params).fetchall()
        ]
        has_more = len(raw_rows) > limit
        page_rows = raw_rows[:limit]

        signals = [_row_to_search_dict(r) for r in page_rows]

        # D3.1: attach recent_changes per signal (single IN query).
        self._attach_recent_changes(signals, since)

        # D3.1: total_token_estimate across the page.
        total_token_estimate = sum(
            (s.get("body_token_estimate") or 0) for s in signals
        )

        next_cursor: Optional[str] = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self._encode_cursor(
                str(last["last_synced_at"]), str(last["signal_id"])
            )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "signals": signals,
            "pagination": {
                "total": total,
                "returned": len(signals),
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
            "meta": {
                "query_since": since,
                "query_time_ms": elapsed_ms,
                "total_token_estimate": total_token_estimate,
            },
        }

    # ── feed helpers ───────────────────────────────────────

    def _attach_recent_changes(
        self, signals: list[dict[str, Any]], since: str
    ) -> None:
        """Batch-load meaningful changes and attach as ``recent_changes``.

        Single ``IN (…)`` query against ``signal_changes`` — O(1) round
        trips regardless of page size. Mutates each signal dict in place.
        """
        if not signals:
            return

        sids = [s["signal_id"] for s in signals]
        placeholders = ",".join("?" * len(sids))
        rows = self._conn.execute(
            f"SELECT signal_id, change_type, changed_at, old_value, new_value "
            f"FROM signal_changes "
            f"WHERE signal_id IN ({placeholders}) "
            f"  AND detected_at >= ? "
            f"  AND is_meaningful = 1 "
            f"ORDER BY detected_at DESC",
            [*sids, since],
        ).fetchall()

        changes_by_sid: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            row = dict(r)
            sid = row.pop("signal_id")
            changes_by_sid.setdefault(sid, []).append(row)

        for s in signals:
            s["recent_changes"] = changes_by_sid.get(s["signal_id"], [])

    # ── cursor helpers ─────────────────────────────────────

    @staticmethod
    def _encode_cursor(last_synced_at: str, signal_id: str) -> str:
        """Pack ``(last_synced_at, signal_id)`` into an opaque URL-safe
        base64 string. ``|`` is used as a separator — neither field
        contains it in our ID / timestamp formats.
        """
        plain = f"{last_synced_at}|{signal_id}".encode("utf-8")
        return base64.urlsafe_b64encode(plain).decode("ascii")

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Inverse of :meth:`_encode_cursor`. Returns ``(None, None)`` on
        empty / malformed input so callers can short-circuit.
        """
        if not cursor:
            return None, None
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None, None
        if "|" not in raw:
            return None, None
        ts, sid = raw.split("|", 1)
        return ts, sid
