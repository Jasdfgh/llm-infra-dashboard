#!/usr/bin/env bash
# Teams alert via Power Automate Workflows webhook.
# Webhook URL is read from secrets/teams_webhook_url.txt (one line, the full URL).
# If the file doesn't exist, alert is silently skipped (best-effort).

set -euo pipefail
# Note: individual steps use explicit error handling (|| exit 0, || echo fallback)
# so -e is safe here — best-effort logic is handled per-command, not globally.

WORKSHOP="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$WORKSHOP/src" ] && [ -d "$(dirname "$WORKSHOP")/data" ]; then
    ROOT="$(dirname "$WORKSHOP")"
else
    ROOT="$WORKSHOP"
fi
WEBHOOK_URL_FILE="$ROOT/secrets/teams_webhook_url.txt"
UNIT_NAME="${1:-unknown-unit}"
HOSTNAME=$(hostname)
TIMESTAMP=$(date -Iseconds)

# Best-effort: no webhook URL configured → skip silently
if [ ! -f "$WEBHOOK_URL_FILE" ]; then
    echo "No Teams webhook URL configured at $WEBHOOK_URL_FILE — skipping alert."
    exit 0
fi

WEBHOOK_URL=$(head -1 "$WEBHOOK_URL_FILE" | tr -d '[:space:]')

if [ -z "$WEBHOOK_URL" ]; then
    echo "Teams webhook URL is empty — skipping alert."
    exit 0
fi

# ── Detect service type from unit name ──
IS_SYNC=false
case "$UNIT_NAME" in
    signals-sync.service) IS_SYNC=true ;;
    *) IS_SYNC=false ;;
esac

DB="$ROOT/data/signals.db"

# ── Consecutive failure threshold: only for sync service (has sync_runs table) ──
if $IS_SYNC; then
    CONSECUTIVE_THRESHOLD=2
    if [ -f "$DB" ] && command -v sqlite3 &>/dev/null; then
        RECENT_FAILURES=$(sqlite3 "$DB" "
            SELECT COUNT(*) FROM (
                SELECT status FROM sync_runs
                WHERE status != 'running'
                ORDER BY started_at DESC
                LIMIT $CONSECUTIVE_THRESHOLD
            ) WHERE status != 'completed'
        " 2>/dev/null || echo "0")

        if [ "$RECENT_FAILURES" -lt "$CONSECUTIVE_THRESHOLD" ] 2>/dev/null; then
            echo "Only $RECENT_FAILURES consecutive failure(s) (threshold=$CONSECUTIVE_THRESHOLD) — suppressing alert."
            exit 0
        fi
    fi
fi

# Build summary depending on service type
SUMMARY=""
FAIL_COUNT="?"
LAST_OK="N/A"

if $IS_SYNC && [ -f "$DB" ] && command -v sqlite3 &>/dev/null; then
    LAST_OK=$(sqlite3 "$DB" "
        SELECT source_repo || ' (' || completed_at || ')'
        FROM sync_runs
        WHERE status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
    " 2>/dev/null || echo "unknown")

    FAIL_COUNT=$(sqlite3 "$DB" "
        SELECT COUNT(*) FROM (
            SELECT status FROM sync_runs
            WHERE status != 'running'
            ORDER BY started_at DESC
            LIMIT 10
        ) WHERE status != 'completed'
    " 2>/dev/null || echo "?")

    FAILED_REPOS=$(sqlite3 -separator ' | ' "$DB" "
        SELECT source_repo, COALESCE(SUBSTR(error_message, 1, 80), status)
        FROM sync_runs
        WHERE status NOT IN ('completed', 'running')
        ORDER BY started_at DESC
        LIMIT 3
    " 2>/dev/null || echo "unknown")

    SUMMARY="Consecutive failures: ${FAIL_COUNT}\nLast success: ${LAST_OK}\nRecent failures:\n${FAILED_REPOS}"
else
    NRESTARTS=$(systemctl --user show -p NRestarts --value "$UNIT_NAME" 2>/dev/null || echo "?")
    JOURNAL=$(journalctl --user -u "$UNIT_NAME" -n 10 --no-pager 2>/dev/null | tail -5 || echo "(journal unavailable)")
    FAIL_COUNT="$NRESTARTS"
    LAST_OK="(not applicable)"
    SUMMARY="NRestarts: ${NRESTARTS}\nRecent journal:\n${JOURNAL}"
fi
SUMMARY_JSON=$(printf '%s' "$SUMMARY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[:2000]))' 2>/dev/null || echo '"(summary encoding failed)"')

# Build Adaptive Card JSON
PAYLOAD=$(cat <<EOFCARD
{
  "type": "message",
  "attachments": [{
    "contentType": "application/vnd.microsoft.card.adaptive",
    "content": {
      "type": "AdaptiveCard",
      "\$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
      "version": "1.4",
      "body": [
        {
          "type": "TextBlock",
          "text": "⚠️ Service Failure Alert",
          "weight": "Bolder",
          "size": "Large",
          "color": "Attention"
        },
        {
          "type": "FactSet",
          "facts": [
            {"title": "Service", "value": "${UNIT_NAME}"},
            {"title": "Host", "value": "${HOSTNAME}"},
            {"title": "Time", "value": "${TIMESTAMP}"},
            {"title": "Consecutive failures", "value": "${FAIL_COUNT}"}
          ]
        },
        {
          "type": "TextBlock",
          "text": "Details:",
          "weight": "Bolder",
          "spacing": "Medium"
        },
        {
          "type": "TextBlock",
          "text": $SUMMARY_JSON,
          "wrap": true,
          "fontType": "Monospace",
          "size": "Small"
        }
      ]
    }
  }]
}
EOFCARD
)

# Send to Teams (5s timeout, best-effort)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 5 \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "202" ]; then
    echo "Teams alert sent for $UNIT_NAME (HTTP $HTTP_CODE)"
else
    echo "Teams alert failed for $UNIT_NAME (HTTP $HTTP_CODE)"
fi
