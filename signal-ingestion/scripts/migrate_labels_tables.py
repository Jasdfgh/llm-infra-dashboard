#!/usr/bin/env python3
"""One-time migration: ensure signal_labels + signal_gap_ids are populated.

Delegates all DDL (tables, FK, triggers, indexes) and backfill to init_db(),
then prints row counts for verification.

Usage:
    .venv/bin/python scripts/migrate_labels_tables.py
    .venv/bin/python scripts/migrate_labels_tables.py --db-path /path/to/signals.db
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.storage.database import get_connection, init_db


_CANONICAL_SCHEMAS = {
    "signal_labels": {
        "pk_columns": {"signal_id", "label"},
        "fk": {"table": "signals", "from": "signal_id", "to": "signal_id", "on_delete": "CASCADE"},
    },
    "signal_gap_ids": {
        "pk_columns": {"signal_id", "gap_id"},
        "fk": {"table": "signals", "from": "signal_id", "to": "signal_id", "on_delete": "CASCADE"},
    },
}


def _check_and_rebuild_helper_tables(conn: sqlite3.Connection) -> bool:
    """Check if signal_labels / signal_gap_ids match the canonical schema.
    Validates exact composite PK columns and FK with ON DELETE CASCADE.
    Returns True if any table was rebuilt."""
    rebuilt = False
    for table, expected in _CANONICAL_SCHEMAS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            continue

        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        pk_cols = {c[1] for c in cols if c[5] > 0}

        fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        fk_ok = False
        for fk in fks:
            if (fk[2] == expected["fk"]["table"]
                    and fk[3] == expected["fk"]["from"]
                    and fk[4] == expected["fk"]["to"]
                    and fk[6] == expected["fk"]["on_delete"]):
                fk_ok = True
                break

        if pk_cols != expected["pk_columns"] or not fk_ok:
            print(f"  {table}: schema mismatch (pk={pk_cols}, fk_ok={fk_ok}), rebuilding...")
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            rebuilt = True

    if rebuilt:
        for trigger in (
            "signals_labels_ai", "signals_labels_au", "signals_labels_ad",
            "signals_gaps_ai", "signals_gaps_au", "signals_gaps_ad",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for idx in ("idx_signal_labels_label", "idx_signal_gap_ids_gap"):
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
        conn.commit()

    return rebuilt


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Database: {db_path}")
    t0 = time.time()

    pre_conn = get_connection(db_path)
    try:
        rebuilt = _check_and_rebuild_helper_tables(pre_conn)
        if rebuilt:
            print("  Bad helper tables dropped, will be recreated by init_db().")
    finally:
        pre_conn.close()

    init_db(db_path)
    print(f"init_db() complete ({time.time() - t0:.1f}s) — schema + backfill applied.")

    conn = get_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        label_rows = conn.execute("SELECT COUNT(*) FROM signal_labels").fetchone()[0]
        gap_rows = conn.execute("SELECT COUNT(*) FROM signal_gap_ids").fetchone()[0]

        print(f"\nResults:")
        print(f"  signals:       {total}")
        print(f"  signal_labels: {label_rows} rows")
        print(f"  signal_gap_ids: {gap_rows} rows")

        _verify_sample(conn)
    finally:
        conn.close()

    print("\nMigration complete.")


def _verify_sample(conn: sqlite3.Connection) -> None:
    """Spot-check one row to confirm label consistency."""
    import json

    row = conn.execute(
        "SELECT signal_id, github_labels FROM signals "
        "WHERE github_labels IS NOT NULL AND json_valid(github_labels) "
        "AND json_type(github_labels) = 'array' AND github_labels != '[]' "
        "LIMIT 1"
    ).fetchone()
    if not row:
        print("\n  (no rows with valid github_labels to verify)")
        return

    sid, labels_json = row
    db_labels = {
        r[0]
        for r in conn.execute(
            "SELECT label FROM signal_labels WHERE signal_id = ?", (sid,)
        ).fetchall()
    }
    json_labels = set(json.loads(labels_json))
    ok = db_labels == json_labels
    print(f"\n  Verify {sid}: JSON={sorted(json_labels)}, table={sorted(db_labels)}")
    print(f"  {'OK — match!' if ok else 'MISMATCH!'}")
    if not ok:
        sys.exit(2)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default="data/signals.db")
    args = p.parse_args()
    migrate(Path(args.db_path))
