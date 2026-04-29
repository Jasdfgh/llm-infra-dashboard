#!/usr/bin/env bash
# =============================================================================
# One-command status check for all signals infrastructure services.
#
# Usage:  bash scripts/signals_status.sh
# =============================================================================

PROJ="/home/yaywang/my-llm-infra-dashboard"
VENV="$PROJ/.venv/bin/python"
DB="$PROJ/data/signals.db"

echo "═══════════════════════════════════════════════════════════════"
echo "  Signals Infrastructure Status — $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════════"

echo ""
echo "SERVICES"
echo "───────────────────────────────────────────────────────────────"
printf "  %-30s" "dbhub MCP HTTP:"
if systemctl --user is-active signals-dbhub.service >/dev/null 2>&1; then
    uptime=$(systemctl --user show signals-dbhub.service -p ActiveEnterTimestamp --value)
    echo "RUNNING (since $uptime)"
else
    echo "STOPPED"
fi

printf "  %-30s" "MCP Service (query+sync):"
if systemctl --user is-active signals-sync-mcp.service >/dev/null 2>&1; then
    uptime2=$(systemctl --user show signals-sync-mcp.service -p ActiveEnterTimestamp --value)
    echo "RUNNING (since $uptime2)"
else
    echo "STOPPED"
fi

printf "  %-30s" "Sync timer (every 2h):"
if systemctl --user is-active signals-sync.timer >/dev/null 2>&1; then
    next=$(systemctl --user list-timers signals-sync.timer --no-legend 2>/dev/null | awk '{print $1, $2, $3}')
    echo "ACTIVE (next: $next)"
else
    echo "INACTIVE"
fi

printf "  %-30s" "Log rotation (daily):"
if systemctl --user is-active signals-logrotate.timer >/dev/null 2>&1; then
    echo "ACTIVE"
else
    echo "INACTIVE"
fi

echo ""
echo "MCP ENDPOINTS"
echo "───────────────────────────────────────────────────────────────"
ip=$(hostname -I | awk '{print $1}')

# Helper: detect bind address for a port and print the appropriate URL
_mcp_url() {
    local port=$1 label=$2
    local bind
    bind=$(ss -tlnp 2>/dev/null | grep ":${port} " | awk '{print $4}' | head -1)
    if echo "$bind" | grep -q "0.0.0.0"; then
        printf "  %-30s%s\n" "$label" "http://$ip:$port/mcp"
    elif echo "$bind" | grep -q "127.0.0.1" || echo "$bind" | grep -q "\[::\]"; then
        printf "  %-30s%s\n" "$label" "http://localhost:$port/mcp"
    else
        printf "  %-30s%s\n" "$label" "http://localhost:$port/mcp (not detected)"
    fi
}

_mcp_url 8081 "dbhub (read-only SQL):"
printf "  %-30s" "  Health:"
resp=$(curl -sf -m 3 http://localhost:8081/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","method":"tools/list","id":1}' 2>/dev/null)
if [ $? -eq 0 ] && echo "$resp" | grep -q '"tools"'; then
    tool_count=$(echo "$resp" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null)
    echo "OK ($tool_count tools available)"
else
    echo "UNREACHABLE"
fi

_mcp_url 8082 "Signals Service:"
printf "  %-30s" "  Health:"
sid=$(curl -sf -m 3 -D - http://localhost:8082/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"status","version":"1.0"}},"id":1}' 2>/dev/null \
    | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')
if [ -n "$sid" ]; then
    resp2=$(curl -sf -m 3 http://localhost:8082/mcp \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Mcp-Session-Id: $sid" \
        -d '{"jsonrpc":"2.0","method":"tools/list","id":2}' 2>/dev/null)
    if echo "$resp2" | grep -q '"tools"'; then
        tool_count2=$(echo "$resp2" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null)
        echo "OK ($tool_count2 tools available)"
    else
        echo "SESSION OK, tools/list failed"
    fi
else
    echo "UNREACHABLE"
fi

echo ""
echo "DATABASE"
echo "───────────────────────────────────────────────────────────────"
if [ -f "$DB" ]; then
    $VENV -c "
import sqlite3, os
c = sqlite3.connect('$DB')
sigs = c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
coms = c.execute('SELECT COUNT(*) FROM signal_comments').fetchone()[0]
repos = c.execute('SELECT source_repo, COUNT(*) FROM signals GROUP BY source_repo ORDER BY COUNT(*) DESC').fetchall()
last_sync = c.execute('SELECT MAX(started_at) FROM sync_runs').fetchone()[0] or 'never'
sz = round(os.path.getsize('$DB') / 1024 / 1024, 1)
print(f'  Size:          {sz} MB')
print(f'  Signals:       {sigs:,}')
print(f'  Comments:      {coms:,}')
print(f'  Last sync:     {last_sync}')
for r in repos:
    print(f'    {r[0]}: {r[1]:,}')
c.close()
" 2>&1
else
    echo "  DB not found at $DB"
fi

echo ""
echo "TOKEN POOL"
echo "───────────────────────────────────────────────────────────────"
pat_count=$(grep -c "^GITHUB_TOKENS" "$PROJ/.env" 2>/dev/null || echo 0)
app_count=$(grep -c "^GITHUB_APP_.*_ID=" "$PROJ/.env" 2>/dev/null || echo 0)
if [ -f "$PROJ/.env" ]; then
    pats=$(grep "^GITHUB_TOKENS" "$PROJ/.env" 2>/dev/null | tr ',' '\n' | wc -l)
    echo "  PATs:          $pats"
    echo "  GitHub Apps:   $app_count"
    echo "  Total tokens:  $((pats + app_count))"
    echo "  Capacity:      ~$((( pats + app_count ) * 5000))/hr"
else
    echo "  .env not found"
fi

echo ""
echo "LOGS (last lines)"
echo "───────────────────────────────────────────────────────────────"
for log in data/dbhub_server.log data/sync_incremental.log; do
    if [ -f "$PROJ/$log" ]; then
        echo "  [$log]"
        tail -2 "$PROJ/$log" 2>/dev/null | sed 's/^/    /'
    fi
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Commands: systemctl --user {start|stop|restart} signals-dbhub"
echo "            systemctl --user {start|stop|restart} signals-sync-mcp"
echo "            systemctl --user {start|stop} signals-sync.timer"
echo "            bash scripts/incremental_sync.sh  (manual sync)"
echo "═══════════════════════════════════════════════════════════════"
