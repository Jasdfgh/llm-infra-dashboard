#!/usr/bin/env python3
"""CLI — search the signals.db via FTS5 + filters.

Examples::

    # Full-text search
    python scripts/search_signals.py --query "aiter MLA"

    # Repo + state + label filter (no FTS)
    python scripts/search_signals.py --repo vllm-project/vllm --state open --labels rocm

    # Combine everything
    python scripts/search_signals.py --query "ROCm" --labels bug \
        --since 2026-04-01T00:00:00Z --limit 10

    # Show full detail for one signal (body + comments)
    python scripts/search_signals.py --detail github:vllm-project/vllm:issue:39303
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from textwrap import shorten

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.storage.database import DEFAULT_DB_PATH  # noqa: E402
from src.storage.repository import SignalRepository  # noqa: E402
from src.storage.search import SignalSearch  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Search signals.db")
    p.add_argument(
        "--query",
        default=None,
        help=(
            "FTS5 query string "
            "(AND/OR/NOT/phrases; known qualifier typos like "
            "title::foo are normalized)"
        ),
    )
    p.add_argument("--repo", action="append", default=None, help="repo filter (repeatable)")
    p.add_argument(
        "--source-types",
        default="",
        help="comma-separated source types, e.g. github_issue,github_pr",
    )
    p.add_argument(
        "--labels",
        default="",
        help="comma-separated labels filter, e.g. rocm,bug",
    )
    p.add_argument("--state", choices=["open", "closed", "all"], default=None)
    p.add_argument("--since", default=None, help="updated_at >= ISO 8601")
    p.add_argument("--until", default=None, help="updated_at <= ISO 8601")
    p.add_argument(
        "--sort",
        choices=["relevance", "updated", "created"],
        default="updated",
    )
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument(
        "--detail",
        default=None,
        help="show full detail for this signal_id (body + comments)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit raw JSON instead of human table",
    )
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    return p.parse_args()


def _render_table(results: list[dict]) -> None:
    if not results:
        print("(no results)")
        return

    # Column widths
    hdr = ("#", "type", "state", "labels", "updated", "comments", "v", "signal_id")
    widths = [2, 4, 6, 20, 20, 5, 3, 40]
    rows = []
    for r in results:
        labels = r.get("github_labels") or []
        if isinstance(labels, str):
            labels = json.loads(labels) if labels else []
        rows.append(
            (
                str(r.get("source_number") or ""),
                _short_type(r.get("source_type")),
                r.get("github_state") or "-",
                ",".join(labels)[: widths[3]],
                (r.get("updated_at") or "")[:20],
                str(r.get("github_comment_count") or 0),
                str(r.get("version") or 1),
                r.get("signal_id") or "",
            )
        )

    # Auto-expand last column to fit
    widths = list(widths)
    for row in rows:
        widths[-1] = max(widths[-1], len(row[-1]))

    fmt = " ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*hdr))
    print("-" * (sum(widths) + len(widths) - 1))
    for row in rows:
        print(fmt.format(*row))


def _short_type(st: str | None) -> str:
    if not st:
        return "?"
    return {"github_issue": "gh_i", "github_pr": "gh_pr"}.get(st, st[:5])


def _render_detail(detail: dict) -> None:
    print(f"Signal: {detail['signal_id']}")
    print(f"  source:    {detail.get('source_type')} — {detail.get('source_url')}")
    print(f"  title:     {detail.get('title')}")
    print(f"  author:    {detail.get('author')}")
    print(
        f"  created:   {detail.get('created_at')}  "
        f"updated: {detail.get('updated_at')}"
    )
    gh = detail.get("github_state"), detail.get("github_labels"), detail.get("github_comment_count")
    if any(x is not None for x in gh):
        labels = detail.get("github_labels")
        if isinstance(labels, str):
            labels = json.loads(labels) if labels else []
        print(
            f"  state:     {detail.get('github_state')}  labels: {labels}  "
            f"comments: {detail.get('github_comment_count')}  is_pr: {detail.get('github_is_pr')}"
        )
    tags = detail.get("tags")
    if isinstance(tags, str):
        tags = json.loads(tags) if tags else []
    if tags:
        print(f"  tags:      {tags}")
    print(f"  version:   {detail.get('version')}  content_hash: {detail.get('content_hash', '')[:16]}…")
    body = detail.get("body") or ""
    print()
    print("Body:")
    print("  " + body.replace("\n", "\n  "))

    comments = detail.get("comments") or []
    if comments:
        print()
        print(f"Comments ({len(comments)}):")
        for c in comments:
            bot_tag = " [BOT]" if c.get("is_bot") else ""
            body_prev = shorten(c.get("body") or "", width=200, placeholder="…")
            print(
                f"  [{c.get('created_at')}] {c.get('author')}{bot_tag}: {body_prev}"
            )


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"error: database {db_path} does not exist.", file=sys.stderr)
        return 2

    source_types = [x.strip() for x in args.source_types.split(",") if x.strip()] or None
    labels = [x.strip() for x in args.labels.split(",") if x.strip()] or None

    with SignalRepository(db_path) as repo:
        search = SignalSearch(repo)

        if args.detail:
            detail = search.get_detail(args.detail)
            if detail is None:
                print(f"not found: {args.detail}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(detail, ensure_ascii=False, indent=2))
            else:
                _render_detail(detail)
            return 0

        try:
            result = search.search(
                query=args.query,
                repos=args.repo,
                source_types=source_types,
                labels=labels,
                state=args.state,
                since=args.since,
                until=args.until,
                sort=args.sort,
                limit=args.limit,
                offset=args.offset,
            )
        except sqlite3.OperationalError as e:
            print(f"error: search failed — {e}", file=sys.stderr)
            return 1

        fts_error = result.get("meta", {}).get("error")

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if fts_error:
                return 1
        else:
            meta = result.get("meta", {})
            fts_error = meta.get("error")
            if fts_error:
                print(
                    f"error: FTS5 query rejected — {fts_error}",
                    file=sys.stderr,
                )
                if "sanitized" in meta.get("filters_applied", {}):
                    print(
                        f"  sanitized query was: {meta['filters_applied']['sanitized']}",
                        file=sys.stderr,
                    )
                return 1
            total = result.get("total", len(result.get("results", [])))
            q_desc = f'matching "{args.query}"' if args.query else "matching filters"
            print(f"Found {total} signal(s) {q_desc}:")
            _render_table(result.get("results", []))
        return 0


if __name__ == "__main__":
    sys.exit(main())
