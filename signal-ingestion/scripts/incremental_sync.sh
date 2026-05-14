#!/usr/bin/env bash
# =============================================================================
# Incremental sync — pull latest changes from GitHub for all tracked repos.
# Designed to be called by systemd timer every 2 hours.
#
# Usage:
#   bash scripts/incremental_sync.sh
# =============================================================================

set -euo pipefail

WORKSHOP="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$WORKSHOP/src" ] && [ -d "$(dirname "$WORKSHOP")/data" ]; then
    ROOT="$(dirname "$WORKSHOP")"
else
    ROOT="$WORKSHOP"
fi
VENV="$ROOT/.venv/bin/python"
LOGDIR="$ROOT/data"
LOCK_FILE="/tmp/signals-sync.lock"

mkdir -p "$LOGDIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(ts)] Another sync is running, skipping."
    exit 0
fi

FAILURES=0

# Read repo list from config/sources.yaml using project venv (has PyYAML)
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

# Validate repo names (same regex as MCP server)
for repo in "${REPOS[@]}"; do
    if ! [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "[$(ts)] ERROR: Invalid repo name in config: $repo"
        exit 1
    fi
done

echo "[$(ts)] === Incremental sync started (${#REPOS[@]} repos) ==="

for repo in "${REPOS[@]}"; do
    echo "[$(ts)] Syncing $repo..."

    if ! $VENV "$WORKSHOP/scripts/sync_github.py" \
        --repo "$repo" \
        --labels "" \
        --mode incremental \
        --include-comments \
        --max-comments 100 \
        --log-level WARNING \
        2>&1; then
        echo "[$(ts)] WARNING: $repo sync failed"
        FAILURES=$((FAILURES + 1))
    fi

    echo "[$(ts)] $repo done."
done

# Post-sync maintenance
echo "[$(ts)] Running WAL checkpoint..."
if ! $VENV -c "
import sqlite3
c = sqlite3.connect('$ROOT/data/signals.db')
c.execute('PRAGMA busy_timeout = 30000')
c.execute('PRAGMA wal_checkpoint(PASSIVE)')
c.execute('ANALYZE')
c.close()
print('  checkpoint + ANALYZE done')
" 2>&1; then
    echo "[$(ts)] WARNING: maintenance failed"
    FAILURES=$((FAILURES + 1))
fi

echo "[$(ts)] === Incremental sync finished (failures=$FAILURES) ==="
exit $FAILURES
