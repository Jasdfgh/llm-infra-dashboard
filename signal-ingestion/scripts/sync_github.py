#!/usr/bin/env python3
"""CLI — trigger a GitHub sync run.

Examples::

    # Incremental pull of rocm-labeled issues + PRs in vllm
    python scripts/sync_github.py --repo vllm-project/vllm --labels rocm

    # Full sync (ignore last-sync timestamp)
    python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full

    # Pin a specific `since` timestamp
    python scripts/sync_github.py --repo vllm-project/vllm --labels rocm \
        --since 2026-04-14T00:00:00Z

    # Target specific issue/PR numbers
    python scripts/sync_github.py --repo vllm-project/vllm \
        --mode targeted --target 39303,39616

Outputs (stdout): a human-readable progress trace matching D8's demo flow.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ingestion.models import SyncMode  # noqa: E402
from src.storage.database import DEFAULT_DB_PATH, init_db  # noqa: E402
from src.sync.orchestrator import build_default_orchestrator  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync GitHub repo into signals.db")
    p.add_argument("--repo", required=True, help="owner/name, e.g. vllm-project/vllm")
    p.add_argument(
        "--mode",
        default="incremental",
        choices=["incremental", "full", "targeted"],
        help="sync mode (default: incremental)",
    )
    p.add_argument(
        "--labels",
        default="",
        help="comma-separated labels filter, e.g. 'rocm' or 'rocm,amd'",
    )
    p.add_argument(
        "--state",
        default="all",
        choices=["all", "open", "closed"],
        help="issue/PR state filter (default: all)",
    )
    p.add_argument(
        "--since",
        default=None,
        help="ISO 8601 UTC timestamp; overrides last-sync-based incremental",
    )
    p.add_argument(
        "--target",
        default="",
        help="comma-separated issue/PR numbers for targeted mode, e.g. 39303,39616",
    )
    p.add_argument("--max-pages", type=int, default=None, help="cap pages fetched")
    p.add_argument(
        "--include-comments",
        action="store_true",
        default=True,
        help="fetch comments on change (default: true)",
    )
    p.add_argument(
        "--no-include-comments",
        dest="include_comments",
        action="store_false",
        help="disable comment fetching",
    )
    p.add_argument(
        "--max-comments",
        type=int,
        default=100,
        help="max comments per issue/PR (default: 100)",
    )
    p.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="path to signals.db (default: data/signals.db)",
    )
    p.add_argument(
        "--cache-dir",
        default="data/cache",
        help="path to JSON cache root (default: data/cache)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    p.add_argument(
        "--auto-init-db",
        action="store_true",
        help="run init_db() if the database file is missing",
    )
    return p.parse_args()


def _format_progress(progress, last_id: str) -> str:
    return (
        f"  seen={progress.signals_seen} new={progress.signals_new} "
        f"updated={progress.signals_updated} unchanged={progress.signals_unchanged} "
        f"comments={progress.comments_fetched} "
        f"last={last_id}"
    )


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db_path)
    if not db_path.exists():
        if args.auto_init_db:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"[init_db] creating schema at {db_path}")
            init_db(db_path)
        else:
            print(
                f"error: database {db_path} does not exist. "
                f"Run `scripts/init_db.py` first, or pass --auto-init-db.",
                file=sys.stderr,
            )
            return 2

    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    target_numbers = (
        [int(x.strip()) for x in args.target.split(",") if x.strip()]
        if args.target
        else None
    )
    sync_mode = SyncMode(args.mode)

    orchestrator, repository = build_default_orchestrator(
        db_path=db_path, cache_dir=args.cache_dir,
    )
    if hasattr(orchestrator.adapter, "_token_pool") and orchestrator.adapter._token_pool:
        print(f"  token_pool: {orchestrator.adapter._token_pool.pool_size} token(s)")

    last_preview: list[str] = []

    def on_progress(progress, last_id: str) -> None:
        # Print incrementally every 5 signals to avoid spam
        if progress.signals_seen % 5 == 0 or progress.signals_seen <= 3:
            print(_format_progress(progress, last_id))
        last_preview.append(last_id)

    print(f"[{args.mode}] repo={args.repo} labels={labels} state={args.state}")
    if args.since:
        print(f"  since={args.since}")
    if target_numbers:
        print(f"  target_numbers={target_numbers}")

    try:
        run = await orchestrator.run(
            source_repo=args.repo,
            sync_mode=sync_mode,
            labels=labels,
            state=args.state,
            since=args.since,
            target_numbers=target_numbers,
            include_comments=args.include_comments,
            max_comments_per_issue=args.max_comments,
            max_pages=args.max_pages,
            progress_callback=on_progress,
        )
    finally:
        await orchestrator.adapter.aclose()
        repository.close()

    # Final summary
    print()
    print(f"Sync {run.status}: {run.id}")
    print(
        f"  total={run.signals_total} "
        f"created={run.signals_created} "
        f"updated={run.signals_updated} "
        f"unchanged={run.signals_unchanged}"
    )
    print(f"  comments_fetched={run.comments_fetched}")
    print(f"  api_calls_used={run.api_calls_used}")
    if run.error_message:
        print(f"  error: {run.error_message}")
    return 0 if run.status in ("completed",) else 1


def main() -> int:
    return asyncio.run(_main_async(_parse_args()))


if __name__ == "__main__":
    sys.exit(main())
