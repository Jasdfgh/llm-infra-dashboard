#!/usr/bin/env bash
# =============================================================================
# Full sync all repos from config/sources.yaml → run benchmark → generate report
#
# Usage: nohup bash scripts/full_sync_and_report.sh > data/full_sync_master.log 2>&1 &
#
# Runs sequentially (SQLite single-writer constraint); generates report on completion.
# =============================================================================

set -euo pipefail

WORKSHOP="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$WORKSHOP/src" ] && [ -d "$(dirname "$WORKSHOP")/data" ]; then
    ROOT="$(dirname "$WORKSHOP")"
else
    ROOT="$WORKSHOP"
fi
VENV="$ROOT/.venv/bin/python"
DATA="$ROOT/data"
REPORT="$ROOT/data/reports/full_sync_report.md"
LOCK_FILE="/tmp/signals-sync.lock"

mkdir -p "$DATA" "$(dirname "$REPORT")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(ts)] Another sync is running, aborting."
    exit 1
fi

PIPELINE_START=$(date +%s)

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

# ─── Read repos from config/sources.yaml ─────────────────────────
repo_output=$("$VENV" -c "
import yaml, sys
try:
    cfg = yaml.safe_load(open('$WORKSHOP/config/sources.yaml'))
    for r in cfg.get('github', {}).get('repos', []):
        if isinstance(r, dict) and 'repo' in r:
            print(r['repo'])
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)
rc=$?

if [ "$rc" -ne 0 ] || [ -z "$repo_output" ]; then
    echo "[$(ts)] ERROR: Could not read repos from config/sources.yaml (rc=$rc). Fix config and retry."
    exit 1
fi

mapfile -t REPOS <<< "$repo_output"

for repo in "${REPOS[@]}"; do
    if ! [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "[$(ts)] ERROR: Invalid repo name in config: $repo"
        exit 1
    fi
done

TOTAL=${#REPOS[@]}
echo "[$(ts)] Config loaded: $TOTAL repos to sync"
echo ""

# ─── Sync all repos ──────────────────────────────────────────────
succeeded=()
failed=()
declare -A REPO_TIMES

for i in "${!REPOS[@]}"; do
    repo="${REPOS[$i]}"
    phase=$((i + 1))
    log_name="sync_${repo//\//_}_full.log"

    echo "================================================================"
    echo "[Phase $phase/$TOTAL] $repo FULL sync — started $(ts)"
    echo "================================================================"

    REPO_START=$(date +%s)

    if $VENV "$WORKSHOP/scripts/sync_github.py" \
        --repo "$repo" \
        --labels "" \
        --mode full \
        --include-comments \
        --max-comments 100 \
        --auto-init-db \
        --log-level INFO \
        2>&1 | tee "$DATA/$log_name"; then
        succeeded+=("$repo")
    else
        failed+=("$repo")
    fi

    REPO_END=$(date +%s)
    REPO_ELAPSED=$((REPO_END - REPO_START))
    REPO_HOURS=$(echo "scale=2; $REPO_ELAPSED / 3600" | bc)
    REPO_TIMES["$repo"]=$REPO_ELAPSED

    echo ""
    echo "[Phase $phase/$TOTAL] $repo — done in ${REPO_ELAPSED}s (${REPO_HOURS}h)"
    echo ""
done

# ─── Sync summary ────────────────────────────────────────────────
echo "================================================================"
echo "  SYNC SUMMARY"
echo "  Succeeded: ${#succeeded[@]}/$TOTAL | Failed: ${#failed[@]}/$TOTAL"
echo "================================================================"
if [ ${#failed[@]} -gt 0 ]; then
    echo "  Failed repos:"
    for r in "${failed[@]}"; do
        echo "    - $r"
    done
fi
echo ""

# ─── Final DB stats ──────────────────────────────────────────────
echo "================================================================"
echo "[Post-sync] Final DB stats ($(ts))"
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

# ─── Post-sync maintenance ────────────────────────────────────────
echo "[Post-sync] ANALYZE + WAL checkpoint ($(ts))"
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

# ─── Benchmark ────────────────────────────────────────────────────
echo "================================================================"
echo "[Benchmark] Running benchmark ($(ts))"
echo "================================================================"
BENCH_START=$(date +%s)

$VENV "$WORKSHOP/scripts/benchmark_db.py" --db-path "$DATA/signals.db" --iterations 500 \
    2>&1 | tee "$DATA/benchmark_full.log"

BENCH_END=$(date +%s)
BENCH_ELAPSED=$(( BENCH_END - BENCH_START ))
echo ""
echo "[Benchmark] DONE — elapsed ${BENCH_ELAPSED}s"
echo ""

# ─── Generate markdown report ─────────────────────────────────────
TOTAL_END=$(date +%s)
TOTAL_ELAPSED=$(( TOTAL_END - PIPELINE_START ))
TOTAL_HOURS=$(echo "scale=2; $TOTAL_ELAPSED / 3600" | bc)

echo "[Report] Writing report to $REPORT"

# Build timing table rows
TIMING_ROWS=""
for i in "${!REPOS[@]}"; do
    repo="${REPOS[$i]}"
    elapsed=${REPO_TIMES["$repo"]:-0}
    hours=$(echo "scale=2; $elapsed / 3600" | bc)
    status="OK"
    for f in "${failed[@]}"; do
        if [ "$f" = "$repo" ]; then status="FAILED"; break; fi
    done
    TIMING_ROWS+="| $repo | ${elapsed}s (${hours}h) | $status |
"
done

# Build sync log tails
SYNC_LOGS=""
for repo in "${REPOS[@]}"; do
    log_name="sync_${repo//\//_}_full.log"
    SYNC_LOGS+="### ${repo} (last 20 lines)
\`\`\`
$(tail -20 "$DATA/$log_name" 2>/dev/null || echo "log not available")
\`\`\`

"
done

cat > "$REPORT" << REPORTEOF
# Full Sync Report — $(date '+%Y-%m-%d')

> Auto-generated by \`scripts/full_sync_and_report.sh\`
> Machine: $(uname -n) | $(nproc) cores | $(free -h | awk '/Mem:/{print $2}') RAM
> Total pipeline time: ${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)
> Repos: ${#succeeded[@]} succeeded, ${#failed[@]} failed out of $TOTAL

---

## Timing

| Repo | Elapsed | Status |
|---|---|---|
${TIMING_ROWS}| **Benchmark** | **${BENCH_ELAPSED}s** | |
| **Total** | **${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)** | |

## Data Summary

\`\`\`
$FINAL_STATS
\`\`\`

## Benchmark Results

\`\`\`
$(cat "$DATA/benchmark_full.log" 2>/dev/null || echo "benchmark log not available")
\`\`\`

## Sync Logs

${SYNC_LOGS}
---

*Report generated at $(ts)*
REPORTEOF

echo ""
echo "================================================================"
echo "  PIPELINE COMPLETE — $(ts)"
echo "  Total elapsed: ${TOTAL_ELAPSED}s (${TOTAL_HOURS}h)"
echo "  Succeeded: ${#succeeded[@]}/$TOTAL | Failed: ${#failed[@]}/$TOTAL"
echo "  Report: $REPORT"
echo "  Benchmark: $DATA/benchmark_full.log"
echo "================================================================"

if [ "${#failed[@]}" -gt 0 ]; then
    echo "[$(ts)] Exiting with failure: ${#failed[@]} repo(s) failed"
    exit 1
fi
