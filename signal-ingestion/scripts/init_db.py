"""CLI: initialize signals.db with the schema from D2.2.

Usage:
    python scripts/init_db.py [--db-path data/signals.db] [--force]

``--force`` deletes the existing DB file (and WAL sidecars) before rebuilding;
without it the script is a safe, idempotent no-op against an initialized DB.

Design reference: D8 (MVP 4/24 deliverables) lists this script as the first
step of the demo flow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running the script directly (`python scripts/init_db.py`) by putting
# the repo root on sys.path before importing project modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.storage.database import DEFAULT_DB_PATH, get_connection, init_db  # noqa: E402


# Fixed display order matches the D2.2 section layout so docs and CLI output
# stay in sync. Verification treats this as the canonical set of user tables
# (FTS5 shadow tables and sqlite_* internals are filtered out separately).
_EXPECTED_TABLES: tuple[str, ...] = (
    "signals",
    "signals_fts",
    "signal_comments",
    "signal_changes",
    "signal_refs",
    "sync_runs",
    "etag_cache",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize the signals.db SQLite schema (see D2.2).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite DB file (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete any existing DB file before initializing.",
    )
    return parser.parse_args()


def _list_user_tables(db_path: Path) -> list[str]:
    """Return user tables in creation order, excluding FTS5 shadow tables."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'signals_fts_%' "
            "ORDER BY rowid"
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _remove_db_files(db_path: Path) -> None:
    """Delete the DB file and any WAL / SHM / rollback-journal sidecars."""
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def main() -> int:
    args = _parse_args()
    db_path: Path = args.db_path

    if args.force:
        _remove_db_files(db_path)

    init_db(db_path)

    actual = _list_user_tables(db_path)
    missing = set(_EXPECTED_TABLES) - set(actual)
    if missing:
        print(
            f"ERROR: missing tables after init: {sorted(missing)}",
            file=sys.stderr,
        )
        return 1

    # The banner text is load-bearing: D8 demo flow and downstream tests
    # grep for "Created signals.db with 7 tables + FTS5 index".
    print(f"Created signals.db with {len(_EXPECTED_TABLES)} tables + FTS5 index")
    print("Tables: " + ", ".join(actual))
    return 0


if __name__ == "__main__":
    sys.exit(main())
