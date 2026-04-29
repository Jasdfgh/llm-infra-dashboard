#!/usr/bin/env bash
# =============================================================================
# 全量拉取 vllm + sglang → 跑 benchmark → 生成报告
#
# 用法:  nohup bash scripts/full_sync_and_report.sh > data/full_sync_master.log 2>&1 &
#
# 串行执行（SQLite 单写限制），完成后自动出报告。
# =============================================================================

set -euo pipefail

PROJ="/home/yaywang/my-llm-infra-dashboard"
VENV="$PROJ/.venv/bin/python"
DATA="$PROJ/data"
REPORT="$PROJ/thinking/full_sync_report.md"
LOCK_FILE="/tmp/signals-sync.lock"

mkdir -p "$DATA"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(ts)] Another sync is running, aborting."
    exit 1
fi

echo "================================================================"
echo "  FULL SYNC PIPELINE — started $(ts)"
echo "  Server: $(uname -n) | $(nproc) cores | $(free -h | awk '/Mem:/{print $2}') RAM"
echo "================================================================"
echo ""

# ─── Phase 0: DB stats before ────────────────────────────────────
echo "[Phase 0] Pre-sync DB stats ($(ts))"
PRE_STATS=$($VENV -c "
import sqlite3, os
db='$DATA/signals.db'
if not os.path.exists(db):
    print('DB not found — will be created')
else:
    c=sqlite3.connect(db)
    sigs=c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
    coms=c.execute('SELECT COUNT(*) FROM signal_comments').fetchone()[0]
    sz=round(os.path.getsize(db)/1024/1024,1)
    print(f'signals={sigs} comments={coms} db_size={sz}MB')
    c.close()
" 2>&1)
echo "  $PRE_STATS"
echo ""

# ─── Phase 1: vllm full sync ─────────────────────────────────────
echo "================================================================"
echo "[Phase 1] vllm-project/vllm FULL sync — started $(ts)"
echo "================================================================"
VLLM_START=$(date +%s)

$VENV scripts/sync_github.py \
    --repo vllm-project/vllm \
    --labels "" \
    --mode full \
    --include-comments \
    --max-comments 100 \
    --auto-init-db \
    --log-level INFO \
    2>&1 | tee "$DATA/sync_vllm_full.log"

VLLM_END=$(date +%s)
VLLM_ELAPSED=$(( VLLM_END - VLLM_START ))
VLLM_HOURS=$(echo "scale=2; $VLLM_ELAPSED / 3600" | bc)
echo ""
echo "[Phase 1] vllm DONE — elapsed ${VLLM_ELAPSED}s (${VLLM_HOURS}h)"
echo ""

# ─── Phase 1.5: mid-sync stats ───────────────────────────────────
echo "[Phase 1.5] Mid-sync DB stats ($(ts))"
$VENV -c "
import sqlite3, os
c=sqlite3.connect('$DATA/signals.db')
sigs=c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
coms=c.execute('SELECT COUNT(*) FROM signal_comments').fetchone()[0]
chgs=c.execute('SELECT COUNT(*) FROM signal_changes').fetchone()[0]
refs=c.execute('SELECT COUNT(*) FROM signal_refs').fetchone()[0]
sz=round(os.path.getsize('$DATA/signals.db')/1024/1024,1)
print(f'  signals={sigs} comments={coms} changes={chgs} refs={refs} db={sz}MB')
repos=c.execute('SELECT source_repo, COUNT(*) FROM signals GROUP BY source_repo').fetchall()
for r in repos: print(f'    {r[0]}: {r[1]}')
c.close()
" 2>&1
echo ""

# ─── Phase 2: sglang full sync ───────────────────────────────────
echo "================================================================"
echo "[Phase 2] sgl-project/sglang FULL sync — started $(ts)"
echo "================================================================"
SGLANG_START=$(date +%s)

$VENV scripts/sync_github.py \
    --repo sgl-project/sglang \
    --labels "" \
    --mode full \
    --include-comments \
    --max-comments 100 \
    --auto-init-db \
    --log-level INFO \
    2>&1 | tee "$DATA/sync_sglang_full.log"

SGLANG_END=$(date +%s)
SGLANG_ELAPSED=$(( SGLANG_END - SGLANG_START ))
SGLANG_HOURS=$(echo "scale=2; $SGLANG_ELAPSED / 3600" | bc)
echo ""
echo "[Phase 2] sglang DONE — elapsed ${SGLANG_ELAPSED}s (${SGLANG_HOURS}h)"
echo ""

# ─── Phase 3: Final DB stats ─────────────────────────────────────
echo "================================================================"
echo "[Phase 3] Final DB stats ($(ts))"
echo "================================================================"
FINAL_STATS=$($VENV -c "
import sqlite3, os, json
c=sqlite3.connect('$DATA/signals.db')
c.row_factory=sqlite3.Row

sigs=c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
coms=c.execute('SELECT COUNT(*) FROM signal_comments').fetchone()[0]
chgs=c.execute('SELECT COUNT(*) FROM signal_changes').fetchone()[0]
refs=c.execute('SELECT COUNT(*) FROM signal_refs').fetchone()[0]
sz=round(os.path.getsize('$DATA/signals.db')/1024/1024,1)

print(f'Total: signals={sigs} comments={coms} changes={chgs} refs={refs} db={sz}MB')

repos=c.execute('SELECT source_repo, COUNT(*) as cnt FROM signals GROUP BY source_repo ORDER BY cnt DESC').fetchall()
for r in repos: print(f'  {r[\"source_repo\"]}: {r[\"cnt\"]}')

states=c.execute('SELECT github_state, COUNT(*) as cnt FROM signals GROUP BY github_state ORDER BY cnt DESC').fetchall()
for s in states: print(f'  state {s[\"github_state\"]}: {s[\"cnt\"]}')

types=c.execute('SELECT source_type, COUNT(*) as cnt FROM signals GROUP BY source_type ORDER BY cnt DESC').fetchall()
for t in types: print(f'  type {t[\"source_type\"]}: {t[\"cnt\"]}')

avg_body=c.execute('SELECT AVG(LENGTH(body)) FROM signals').fetchone()[0]
max_body=c.execute('SELECT MAX(LENGTH(body)) FROM signals').fetchone()[0]
null_body=c.execute('SELECT COUNT(*) FROM signals WHERE body IS NULL OR body=\"\"').fetchone()[0]
print(f'  body: avg_len={int(avg_body or 0)} max_len={int(max_body or 0)} null/empty={null_body}')

avg_comments=c.execute('SELECT AVG(cnt) FROM (SELECT signal_id, COUNT(*) as cnt FROM signal_comments GROUP BY signal_id)').fetchone()[0]
print(f'  comments/signal: avg={round(avg_comments or 0, 1)}')

cache_count=0
cache_size=0
import pathlib
cache_dir=pathlib.Path('$DATA/cache')
if cache_dir.exists():
    for f in cache_dir.rglob('*.json'):
        cache_count+=1
        cache_size+=f.stat().st_size
print(f'  cache: {cache_count} files, {round(cache_size/1024/1024,1)}MB')

c.close()
" 2>&1)
echo "$FINAL_STATS"
echo ""

# ─── Phase 4: ANALYZE + WAL checkpoint ───────────────────────────
echo "[Phase 4] Post-sync maintenance ($(ts))"
$VENV -c "
import sqlite3, time
c=sqlite3.connect('$DATA/signals.db')
t0=time.time()
c.execute('ANALYZE')
t1=time.time()
print(f'  ANALYZE: {t1-t0:.2f}s')
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
t2=time.time()
print(f'  WAL checkpoint: {t2-t1:.2f}s')
c.close()
" 2>&1
echo ""

# ─── Phase 5: Benchmark ──────────────────────────────────────────
echo "================================================================"
echo "[Phase 5] Running benchmark ($(ts))"
echo "================================================================"
BENCH_START=$(date +%s)

$VENV scripts/benchmark_db.py --db-path "$DATA/signals.db" --iterations 500 \
    2>&1 | tee "$DATA/benchmark_full.log"

BENCH_END=$(date +%s)
BENCH_ELAPSED=$(( BENCH_END - BENCH_START ))
echo ""
echo "[Phase 5] Benchmark DONE — elapsed ${BENCH_ELAPSED}s"
echo ""

# ─── Phase 6: Generate markdown report ───────────────────────────
TOTAL_END=$(date +%s)
TOTAL_ELAPSED=$(( TOTAL_END - VLLM_START ))
TOTAL_HOURS=$(echo "scale=2; $TOTAL_ELAPSED / 3600" | bc)

echo "[Phase 6] Writing report to $REPORT"

cat > "$REPORT" << REPORTEOF
# Full Sync Report — $(date '+%Y-%m-%d')

> Auto-generated by \`scripts/full_sync_and_report.sh\`
> Machine: $(uname -n) | $(nproc) cores | $(free -h | awk '/Mem:/{print $2}') RAM
> Total pipeline time: ${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)

---

## Timing

| Phase | Elapsed | Notes |
|---|---|---|
| vllm full sync | ${VLLM_ELAPSED}s (${VLLM_HOURS}h) | \`--labels "" --mode full --max-comments 100\` |
| sglang full sync | ${SGLANG_ELAPSED}s (${SGLANG_HOURS}h) | \`--labels "" --mode full --max-comments 100\` |
| Benchmark | ${BENCH_ELAPSED}s | 500 iterations |
| **Total** | **${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)** | |

## Data Summary

\`\`\`
$FINAL_STATS
\`\`\`

## Benchmark Results

\`\`\`
$(cat "$DATA/benchmark_full.log" 2>/dev/null || echo "benchmark log not available")
\`\`\`

## Sync Logs (tail)

### vllm (last 30 lines)
\`\`\`
$(tail -30 "$DATA/sync_vllm_full.log" 2>/dev/null || echo "log not available")
\`\`\`

### sglang (last 30 lines)
\`\`\`
$(tail -30 "$DATA/sync_sglang_full.log" 2>/dev/null || echo "log not available")
\`\`\`

## vs Week 2 Scale Plan Predictions

Compare with \`thinking/week2_scale_test_plan.md\` §2.2:

| Metric | Predicted (40K) | Actual | Ratio |
|---|---|---|---|
| DB size | ~480 MB | _(fill from above)_ | |
| signals count | ~40,000 | _(fill from above)_ | |
| FTS5 search | 0.08-0.15ms | _(fill from benchmark)_ | |
| GROUP BY aggregate | 50-100ms | _(fill from benchmark)_ | |

---

*Report generated at $(ts)*
REPORTEOF

echo ""
echo "================================================================"
echo "  PIPELINE COMPLETE — $(ts)"
echo "  Total elapsed: ${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)"
echo "  Report: $REPORT"
echo "  Benchmark: $DATA/benchmark_full.log"
echo "================================================================"
