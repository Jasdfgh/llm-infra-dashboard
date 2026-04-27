#!/usr/bin/env python3
"""CLI — list signal_changes entries.

Examples::

    # All meaningful changes in the last 24h
    python scripts/show_changes.py --since 2026-04-24T00:00:00Z

    # Changes for a specific signal
    python scripts/show_changes.py --signal-id github:vllm-project/vllm:issue:39303

    # Include noise (assignee changes, bot comments, …)
    python scripts/show_changes.py --include-noise --since 2026-04-01T00:00:00Z

Output format matches the D8 demo:

    [YYYY-MM-DD HH:MM] CHANGE_TYPE #NNN: old → new
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.storage.database import DEFAULT_DB_PATH  # noqa: E402
from src.storage.repository import SignalRepository  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Show signal_changes entries")
    p.add_argument(
        "--signal-id",
        default=None,
        help="only show changes for this signal_id (optional)",
    )
    p.add_argument(
        "--since",
        default=None,
        help="only changes detected after this ISO 8601 timestamp",
    )
    p.add_argument(
        "--include-noise",
        action="store_true",
        help="include is_meaningful=0 events (assignees, minor edits)",
    )
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument(
        "--json",
        action="store_true",
        help="emit raw JSON instead of human lines",
    )
    return p.parse_args()


def _short_signal(signal_id: str) -> str:
    """Turn `github:vllm-project/vllm:issue:39303` into `#39303`."""
    parts = signal_id.split(":")
    if len(parts) >= 4:
        return f"#{parts[-1]}"
    return signal_id


def _format_value(raw: str | None) -> str:
    if raw is None:
        return "-"
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(v, list):
        return "[" + ",".join(map(str, v)) + "]"
    return str(v)


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"error: database {db_path} does not exist.", file=sys.stderr)
        return 2

    with SignalRepository(db_path) as repo:
        changes = repo.get_changes(
            signal_id=args.signal_id,
            since=args.since,
            meaningful_only=not args.include_noise,
            limit=args.limit,
        )

    if args.json:
        print(json.dumps(changes, ensure_ascii=False, indent=2, default=str))
        return 0

    if not changes:
        print("(no changes)")
        return 0

    qualifier = "meaningful " if not args.include_noise else ""
    prefix = "since " + args.since if args.since else ""
    header = f"{len(changes)} {qualifier}change(s)"
    if prefix:
        header += " " + prefix
    header += ":"
    print(header)
    for c in changes:
        ts = (c.get("detected_at") or "")[:16].replace("T", " ")
        short = _short_signal(c.get("signal_id") or "")
        ct = c.get("change_type", "?").upper()
        old = _format_value(c.get("old_value"))
        new = _format_value(c.get("new_value"))
        tag = "" if c.get("is_meaningful") else " (noise)"
        print(f"  [{ts}] {ct} {short}: {old} -> {new}{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
