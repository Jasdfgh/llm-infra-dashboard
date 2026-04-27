#!/usr/bin/env python3
"""Performance benchmark for signals.db — hard metrics for Module 1 & 3.

Usage:
    .venv/bin/python scripts/benchmark_db.py --db-path data/signals.db
    .venv/bin/python scripts/benchmark_db.py --iterations 500
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.database import get_connection, init_db, dict_row_factory


# ─────────────────────── timing helpers ───────────────────────


def _bench(func, n: int) -> dict[str, float]:
    """Run *func* n times, return latency stats in milliseconds."""
    timings: list[float] = []
    errors = 0
    for _ in range(n):
        t0 = time.perf_counter_ns()
        try:
            func()
        except Exception:
            errors += 1
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
        timings.append(elapsed_ms)
    if not timings:
        return {"avg": 0, "p50": 0, "p95": 0, "p99": 0, "errors": errors}
    timings.sort()
    return {
        "avg": statistics.mean(timings),
        "p50": timings[len(timings) // 2],
        "p95": timings[int(len(timings) * 0.95)],
        "p99": timings[int(len(timings) * 0.99)],
        "errors": errors,
    }


def _fmt_row(label: str, stats: dict[str, float], width: int = 36) -> str:
    return (
        f"  {label:<{width}}"
        f"avg={stats['avg']:.2f}ms  "
        f"p50={stats['p50']:.2f}ms  "
        f"p95={stats['p95']:.2f}ms  "
        f"p99={stats['p99']:.2f}ms"
    )


# ─────────────────────── storage metrics ──────────────────────


def _file_size_str(path: Path) -> str:
    if not path.exists():
        return "0 KB"
    size = path.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _storage_report(db_path: Path, conn: sqlite3.Connection) -> list[str]:
    lines: list[str] = []
    lines.append(f"  {'signals.db':<24}{_file_size_str(db_path)}")
    wal = db_path.parent / (db_path.name + "-wal")
    lines.append(f"  {'signals.db-wal':<24}{_file_size_str(wal)}")

    rows = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'index') ORDER BY type, name"
    ).fetchall()
    tables = [r["name"] for r in rows if r["type"] == "table"]
    indexes = [r["name"] for r in rows if r["type"] == "index"]

    has_dbstat = False
    try:
        conn.execute("SELECT 1 FROM dbstat LIMIT 1")
        has_dbstat = True
    except Exception:
        pass

    table_pages: dict[str, int] = {}
    for t in tables:
        if has_dbstat:
            try:
                row = conn.execute(
                    "SELECT SUM(pgsize) AS total FROM dbstat WHERE name = ?", (t,)
                ).fetchone()
                table_pages[t] = (row["total"] or 0) if row else 0
            except Exception:
                table_pages[t] = 0
        else:
            table_pages[t] = 0

    index_total = 0
    if has_dbstat:
        for idx in indexes:
            try:
                row = conn.execute(
                    "SELECT SUM(pgsize) AS total FROM dbstat WHERE name = ?", (idx,)
                ).fetchone()
                index_total += (row["total"] or 0) if row else 0
            except Exception:
                pass

    fts_total = 0
    for t in tables:
        if "fts" in t.lower():
            fts_total += table_pages.get(t, 0)

    db_total = db_path.stat().st_size if db_path.exists() else 1

    for t in ("signals", "signal_comments", "signal_changes"):
        if t in table_pages and table_pages[t]:
            sz = table_pages[t]
            pct = sz / db_total * 100
            lines.append(f"  {'table ' + t:<24}{sz / (1024*1024):.1f} MB ({pct:.0f}%)")

    if index_total:
        lines.append(f"  {'index total':<24}{index_total / (1024*1024):.1f} MB")
    if fts_total:
        lines.append(f"  {'FTS5 total':<24}{fts_total / (1024*1024):.1f} MB")

    return lines


# ────────────────────── read benchmarks ───────────────────────


def bench_fts_search(conn: sqlite3.Connection, n: int) -> dict:
    def fn():
        conn.execute(
            "SELECT s.signal_id, s.title FROM signals_fts fts "
            "JOIN signals s ON s.rowid = fts.rowid "
            "WHERE signals_fts MATCH 'aiter MLA' LIMIT 20"
        ).fetchall()
    return _bench(fn, n)


def bench_btree_filter(conn: sqlite3.Connection, n: int) -> dict:
    def fn():
        conn.execute(
            "SELECT signal_id, title FROM signals "
            "WHERE source_repo = ? AND github_state = ?",
            ("vllm-project/vllm", "open"),
        ).fetchall()
    return _bench(fn, n)


def bench_get_by_id(conn: sqlite3.Connection, n: int, sample_id: str) -> dict:
    def fn():
        conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (sample_id,)
        ).fetchone()
    return _bench(fn, n)


def bench_feed(conn: sqlite3.Connection, n: int) -> dict:
    def fn():
        conn.execute("SELECT * FROM signals LIMIT 500").fetchall()
    return _bench(fn, n)


def bench_join(conn: sqlite3.Connection, n: int, sample_id: str) -> dict:
    def fn():
        conn.execute(
            "SELECT s.signal_id, s.title, c.comment_id, c.body "
            "FROM signals s "
            "JOIN signal_comments c ON c.signal_id = s.signal_id "
            "WHERE s.signal_id = ?",
            (sample_id,),
        ).fetchall()
    return _bench(fn, n)


def bench_aggregate(conn: sqlite3.Connection, n: int) -> dict:
    def fn():
        conn.execute(
            "SELECT source_repo, github_state, COUNT(*) as cnt "
            "FROM signals GROUP BY source_repo, github_state"
        ).fetchall()
    return _bench(fn, n)


# ────────────────────── write benchmarks ──────────────────────


def _make_tmp_db(db_path: Path, tmp_dir: str) -> Path:
    """Copy production DB to a temp location for write tests."""
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = Path(tmp_dir) / "bench_signals.db"
    shutil.copy2(db_path, tmp_path)
    wal = db_path.parent / (db_path.name + "-wal")
    if wal.exists():
        shutil.copy2(wal, Path(tmp_dir) / (tmp_path.name + "-wal"))
    return tmp_path


def bench_single_insert(tmp_db: Path, n: int) -> dict:
    conn = get_connection(tmp_db)
    conn.row_factory = dict_row_factory
    now = "2026-04-27T00:00:00Z"
    h = hashlib.sha256()

    def fn():
        uid = f"bench:insert:{time.perf_counter_ns()}"
        h.update(uid.encode())
        with conn:
            conn.execute(
                "INSERT INTO signals "
                "(signal_id, source_type, source_url, source_repo, "
                "title, body, author, created_at, updated_at, "
                "first_seen_at, last_synced_at, content_hash, version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid, "github_issue",
                    f"https://github.com/test/repo/issues/{uid}",
                    "test/repo",
                    f"Benchmark signal {uid}",
                    "Benchmark body content for performance testing.",
                    "bench_user", now, now, now, now,
                    h.hexdigest(), 1,
                ),
            )
    stats = _bench(fn, n)
    conn.close()
    return stats


def bench_single_update(tmp_db: Path, n: int) -> dict:
    conn = get_connection(tmp_db)
    conn.row_factory = dict_row_factory
    ids = [
        r["signal_id"] for r in conn.execute(
            "SELECT signal_id FROM signals LIMIT ?", (n,)
        ).fetchall()
    ]
    if not ids:
        conn.close()
        return {"avg": 0, "p50": 0, "p95": 0, "p99": 0, "errors": 0}
    idx = [0]

    def fn():
        sid = ids[idx[0] % len(ids)]
        idx[0] += 1
        with conn:
            conn.execute(
                "UPDATE signals SET last_synced_at = ?, version = version + 1 "
                "WHERE signal_id = ?",
                ("2026-04-27T00:00:01Z", sid),
            )
    stats = _bench(fn, n)
    conn.close()
    return stats


def bench_batch_comments(tmp_db: Path, n_batches: int, batch_size: int = 10) -> dict:
    conn = get_connection(tmp_db)
    conn.row_factory = dict_row_factory
    ids = [
        r["signal_id"] for r in conn.execute("SELECT signal_id FROM signals LIMIT 100").fetchall()
    ]
    if not ids:
        conn.close()
        return {"avg": 0, "p50": 0, "p95": 0, "p99": 0, "errors": 0}
    now = "2026-04-27T00:00:00Z"
    counter = [0]

    def fn():
        sid = ids[counter[0] % len(ids)]
        with conn:
            for j in range(batch_size):
                cid = f"bench_c_{counter[0]}_{j}_{time.perf_counter_ns()}"
                conn.execute(
                    "INSERT OR IGNORE INTO signal_comments "
                    "(signal_id, comment_id, author, body, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (sid, cid, "bench_bot", "Benchmark comment.", now),
                )
        counter[0] += 1
    stats = _bench(fn, n_batches)
    conn.close()
    return stats


def bench_atomic_transaction(tmp_db: Path, n: int) -> dict:
    conn = get_connection(tmp_db)
    conn.row_factory = dict_row_factory
    now = "2026-04-27T00:00:00Z"
    counter = [0]

    def fn():
        uid = f"bench:txn:{counter[0]}:{time.perf_counter_ns()}"
        counter[0] += 1
        with conn:
            conn.execute(
                "INSERT INTO signals "
                "(signal_id, source_type, source_url, source_repo, "
                "title, body, author, created_at, updated_at, "
                "first_seen_at, last_synced_at, content_hash, version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid, "github_issue",
                    f"https://github.com/test/repo/issues/{uid}",
                    "test/repo",
                    f"Txn signal {uid}", "body", "bot",
                    now, now, now, now, hashlib.sha256(uid.encode()).hexdigest(), 1,
                ),
            )
            for j in range(3):
                conn.execute(
                    "INSERT OR IGNORE INTO signal_comments "
                    "(signal_id, comment_id, author, body, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (uid, f"{uid}_c{j}", "bot", "comment body", now),
                )
            conn.execute(
                "INSERT INTO signal_changes "
                "(signal_id, change_type, changed_at, detected_at, is_meaningful) "
                "VALUES (?,?,?,?,?)",
                (uid, "new_signal", now, now, 1),
            )
    stats = _bench(fn, n)
    conn.close()
    return stats


# ─────────────────── concurrency benchmarks ───────────────────


def bench_concurrent_read(db_path: Path, n_threads: int, per_thread: int) -> dict:
    all_timings: list[float] = []
    lock = threading.Lock()
    errors = [0]

    def worker():
        c = get_connection(db_path)
        c.row_factory = dict_row_factory
        local_timings: list[float] = []
        for _ in range(per_thread):
            t0 = time.perf_counter_ns()
            try:
                c.execute(
                    "SELECT signal_id, title FROM signals "
                    "WHERE source_repo = ? LIMIT 50",
                    ("vllm-project/vllm",),
                ).fetchall()
            except Exception:
                with lock:
                    errors[0] += 1
            elapsed = (time.perf_counter_ns() - t0) / 1_000_000
            local_timings.append(elapsed)
        c.close()
        with lock:
            all_timings.extend(local_timings)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_timings.sort()
    n = len(all_timings)
    return {
        "avg": statistics.mean(all_timings) if all_timings else 0,
        "p50": all_timings[n // 2] if all_timings else 0,
        "p95": all_timings[int(n * 0.95)] if all_timings else 0,
        "p99": all_timings[int(n * 0.99)] if all_timings else 0,
        "errors": errors[0],
        "total_ops": n,
    }


def bench_concurrent_rw(db_path: Path) -> dict:
    """1 writer + 5 readers, each doing 100 ops on a temp copy."""
    tmp_dir = tempfile.mkdtemp(prefix="bench_rw_")
    tmp_db = _make_tmp_db(db_path, tmp_dir)
    read_timings: list[float] = []
    write_timings: list[float] = []
    lock = threading.Lock()
    errors = [0]
    now = "2026-04-27T00:00:00Z"

    def reader():
        c = get_connection(tmp_db)
        c.row_factory = dict_row_factory
        local: list[float] = []
        for _ in range(100):
            t0 = time.perf_counter_ns()
            try:
                c.execute(
                    "SELECT * FROM signals WHERE source_repo = ? LIMIT 20",
                    ("vllm-project/vllm",),
                ).fetchall()
            except Exception:
                with lock:
                    errors[0] += 1
            local.append((time.perf_counter_ns() - t0) / 1_000_000)
        c.close()
        with lock:
            read_timings.extend(local)

    def writer():
        c = get_connection(tmp_db)
        c.row_factory = dict_row_factory
        local: list[float] = []
        for i in range(100):
            t0 = time.perf_counter_ns()
            uid = f"bench:rw:{i}:{time.perf_counter_ns()}"
            try:
                with c:
                    c.execute(
                        "INSERT INTO signals "
                        "(signal_id, source_type, source_url, source_repo, "
                        "title, body, author, created_at, updated_at, "
                        "first_seen_at, last_synced_at, content_hash, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            uid, "github_issue",
                            f"https://github.com/test/repo/issues/{uid}",
                            "test/repo", f"rw signal {uid}", "body", "bot",
                            now, now, now, now,
                            hashlib.sha256(uid.encode()).hexdigest(), 1,
                        ),
                    )
            except Exception:
                with lock:
                    errors[0] += 1
            local.append((time.perf_counter_ns() - t0) / 1_000_000)
        c.close()
        with lock:
            write_timings.extend(local)

    threads = [threading.Thread(target=reader) for _ in range(5)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    shutil.rmtree(tmp_dir, ignore_errors=True)

    def _stats(arr: list[float]) -> dict:
        if not arr:
            return {"avg": 0, "p50": 0, "p95": 0, "p99": 0}
        arr.sort()
        n = len(arr)
        return {
            "avg": statistics.mean(arr),
            "p50": arr[n // 2],
            "p95": arr[int(n * 0.95)],
            "p99": arr[int(n * 0.99)],
        }

    return {
        "read": _stats(read_timings),
        "write": _stats(write_timings),
        "errors": errors[0],
    }


# ──────────────────────── main runner ─────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="signals.db performance benchmark")
    parser.add_argument(
        "--db-path", default="data/signals.db",
        help="Path to signals.db (default: data/signals.db)",
    )
    parser.add_argument(
        "--iterations", type=int, default=1000,
        help="Iterations for per-query benchmarks (default: 1000)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    n = args.iterations
    n_heavy = max(n // 10, 10)  # 100 for heavy ops

    conn = get_connection(db_path)
    conn.row_factory = dict_row_factory

    sig_count = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    com_count = conn.execute("SELECT COUNT(*) AS n FROM signal_comments").fetchone()["n"]
    db_size_mb = db_path.stat().st_size / (1024 * 1024)
    sqlite_ver = sqlite3.sqlite_version

    sample_row = conn.execute("SELECT signal_id FROM signals LIMIT 1").fetchone()
    sample_id = sample_row["signal_id"] if sample_row else ""

    sample_with_comments = conn.execute(
        "SELECT s.signal_id FROM signals s "
        "JOIN signal_comments c ON c.signal_id = s.signal_id "
        "GROUP BY s.signal_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    join_id = sample_with_comments["signal_id"] if sample_with_comments else sample_id

    total_errors = 0
    banner = (
        f"\n"
        f"{'═' * 63}\n"
        f"  signals.db Performance Benchmark\n"
        f"  DB: {sig_count} signals, {com_count} comments, {db_size_mb:.1f} MB\n"
        f"  SQLite {sqlite_ver}, WAL mode, FTS5 unicode61\n"
        f"{'═' * 63}\n"
    )
    print(banner)

    # ── READ PERFORMANCE ──────────────────────────────────────
    print("READ PERFORMANCE (on production data)")
    print("─" * 63)

    r1 = bench_fts_search(conn, n)
    total_errors += int(r1["errors"])
    print(_fmt_row(f'FTS5 search "aiter MLA" ×{n}', r1))

    r2 = bench_btree_filter(conn, n)
    total_errors += int(r2["errors"])
    print(_fmt_row(f"repo+state filter ×{n}", r2))

    r3 = bench_get_by_id(conn, n, sample_id)
    total_errors += int(r3["errors"])
    print(_fmt_row(f"get_by_id ×{n}", r3))

    r4 = bench_feed(conn, n_heavy)
    total_errors += int(r4["errors"])
    print(_fmt_row(f"feed (500 rows) ×{n_heavy}", r4))

    r5 = bench_join(conn, n_heavy, join_id)
    total_errors += int(r5["errors"])
    print(_fmt_row(f"signal+comments JOIN ×{n_heavy}", r5))

    r6 = bench_aggregate(conn, n_heavy)
    total_errors += int(r6["errors"])
    print(_fmt_row(f"GROUP BY aggregate ×{n_heavy}", r6))

    # ── WRITE PERFORMANCE ─────────────────────────────────────
    print(f"\nWRITE PERFORMANCE (on temp DB copy)")
    print("─" * 63)

    tmp_dir = tempfile.mkdtemp(prefix="bench_write_")
    tmp_db = _make_tmp_db(db_path, tmp_dir)

    w1 = bench_single_insert(tmp_db, n)
    total_errors += int(w1["errors"])
    print(_fmt_row(f"single INSERT ×{n}", w1))

    tmp_db2 = _make_tmp_db(db_path, tmp_dir + "_upd")
    w2 = bench_single_update(tmp_db2, n)
    total_errors += int(w2["errors"])
    print(_fmt_row(f"single UPDATE ×{n}", w2))

    tmp_db3 = _make_tmp_db(db_path, tmp_dir + "_batch")
    w3 = bench_batch_comments(tmp_db3, n_heavy)
    total_errors += int(w3["errors"])
    print(_fmt_row(f"batch comments (10/batch) ×{n_heavy}", w3))

    tmp_db4 = _make_tmp_db(db_path, tmp_dir + "_txn")
    w4 = bench_atomic_transaction(tmp_db4, n_heavy)
    total_errors += int(w4["errors"])
    print(_fmt_row(f"atomic transaction ×{n_heavy}", w4))

    for d in [tmp_dir, tmp_dir + "_upd", tmp_dir + "_batch", tmp_dir + "_txn"]:
        shutil.rmtree(d, ignore_errors=True)

    # ── CONCURRENCY ───────────────────────────────────────────
    print(f"\nCONCURRENCY (WAL mode)")
    print("─" * 63)

    cr = bench_concurrent_read(db_path, n_threads=10, per_thread=n_heavy)
    total_errors += int(cr["errors"])
    total_ops = cr["total_ops"]
    print(
        f"  {'10-thread concurrent read ×' + str(total_ops):<36}"
        f"errors={int(cr['errors'])}  "
        f"avg={cr['avg']:.2f}ms  "
        f"p50={cr['p50']:.2f}ms  "
        f"p95={cr['p95']:.2f}ms  "
        f"p99={cr['p99']:.2f}ms"
    )

    rw = bench_concurrent_rw(db_path)
    total_errors += int(rw["errors"])
    rd, wr = rw["read"], rw["write"]
    print(
        f"  {'read+write concurrent':<36}"
        f"errors={int(rw['errors'])}  "
        f"read_avg={rd['avg']:.2f}ms  "
        f"write_avg={wr['avg']:.2f}ms"
    )

    # ── STORAGE ───────────────────────────────────────────────
    print(f"\nSTORAGE")
    print("─" * 63)
    for line in _storage_report(db_path, conn):
        print(line)

    conn.close()

    # ── VERDICT ───────────────────────────────────────────────
    print(f"\n{'═' * 63}")
    if total_errors == 0:
        print("  BENCHMARK PASSED  (0 errors)")
        print(f"{'═' * 63}")
        return 0
    else:
        print(f"  BENCHMARK FAILED  ({total_errors} errors)")
        print(f"{'═' * 63}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
