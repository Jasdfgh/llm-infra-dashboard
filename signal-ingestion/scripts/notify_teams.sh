#!/bin/bash
# Teams alert via Power Automate Workflows webhook.
# Webhook URL is read from config/teams_webhook_url.txt (one line, the full URL).
# If the file doesn't exist, alert is silently skipped (best-effort).

set -uo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
WEBHOOK_URL_FILE="$PROJ/config/teams_webhook_url.txt"
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

# ── Consecutive failure threshold: only alert after 2+ consecutive failures ──
DB="$PROJ/data/signals.db"
CONSECUTIVE_THRESHOLD=2

if [ -f "$DB" ] && command -v sqlite3 &>/dev/null; then
    # Count recent consecutive failed/timed-out sync_runs (no successful run in between)
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

# Get recent log lines from the failed unit (best-effort)
RECENT_LOG=$(journalctl --user -u "$UNIT_NAME" -n 10 --no-pager 2>/dev/null | tail -5 || echo "no logs available")
RECENT_LOG_JSON=$(printf '%s' "$RECENT_LOG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[:2000]))' 2>/dev/null || echo '"(log encoding failed)"')

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
            {"title": "Unit", "value": "${UNIT_NAME}"},
            {"title": "Host", "value": "${HOSTNAME}"},
            {"title": "Time", "value": "${TIMESTAMP}"}
          ]
        },
        {
          "type": "TextBlock",
          "text": "Recent logs:",
          "weight": "Bolder",
          "spacing": "Medium"
        },
        {
          "type": "TextBlock",
          "text": $RECENT_LOG_JSON,
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
