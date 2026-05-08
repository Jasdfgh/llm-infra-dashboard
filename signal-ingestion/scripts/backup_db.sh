#!/bin/bash
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
DB="$PROJ/data/signals.db"
BACKUP_DIR="$PROJ/data/backups"
RETENTION_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/signals_${DATE}.db"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] Starting backup..."

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
    echo "[$(ts)] ERROR: Database not found: $DB"
    exit 1
fi

# SQLite online backup (safe even while DB is in use)
sqlite3 "$DB" ".backup '$BACKUP_FILE'"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(ts)] Backup created: $BACKUP_FILE ($BACKUP_SIZE)"

# Prune old backups
PRUNED=$(find "$BACKUP_DIR" -name "signals_*.db" -mtime +$RETENTION_DAYS -delete -print | wc -l)
echo "[$(ts)] Pruned $PRUNED backup(s) older than $RETENTION_DAYS days"

# Count remaining
REMAINING=$(find "$BACKUP_DIR" -name "signals_*.db" | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
echo "[$(ts)] Backups: $REMAINING files, $TOTAL_SIZE total"

echo "[$(ts)] Backup complete."
