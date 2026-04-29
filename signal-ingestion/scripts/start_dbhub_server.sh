#!/usr/bin/env bash
# =============================================================================
# Start dbhub as an HTTP MCP server for internal network access.
#
# Usage:
#   bash scripts/start_dbhub_server.sh          # start (default port 8081)
#   bash scripts/start_dbhub_server.sh stop      # stop
#   bash scripts/start_dbhub_server.sh status    # check if running
#   bash scripts/start_dbhub_server.sh restart    # stop + start
#   PORT=9090 bash scripts/start_dbhub_server.sh  # custom port
# =============================================================================

set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -d "$PROJ/src" ] || [ ! -f "$PROJ/requirements.txt" ]; then
    echo "ERROR: Invalid project root: $PROJ" >&2
    exit 1
fi
NODE="${NODE:-$(which node 2>/dev/null || echo "node")}"
DBHUB_ENTRY="${DBHUB_ENTRY:-$(npm root -g 2>/dev/null)/@bytebase/dbhub/dist/index.js}"
CONFIG="$PROJ/dbhub.toml"
PORT="${PORT:-8081}"
LOGFILE="$PROJ/data/dbhub_server.log"
PIDFILE="$PROJ/data/dbhub_server.pid"

mkdir -p "$PROJ/data"

_pid() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        rm -f "$PIDFILE"
    fi
    return 1
}

do_status() {
    local pid
    if pid=$(_pid); then
        echo "dbhub HTTP running (PID $pid, port $PORT)"
        local bind
        bind=$(ss -tlnp 2>/dev/null | grep ":${PORT} " | awk '{print $4}' | head -1)
        if echo "$bind" | grep -q "0.0.0.0"; then
            echo "  endpoint: http://$(hostname -I | awk '{print $1}'):$PORT/mcp"
        else
            echo "  endpoint: http://localhost:$PORT/mcp"
        fi
        echo "  log: $LOGFILE"
        return 0
    else
        echo "dbhub HTTP not running"
        return 1
    fi
}

do_stop() {
    local pid
    if pid=$(_pid); then
        echo "Stopping dbhub (PID $pid)..."
        kill "$pid"
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid"
        fi
        rm -f "$PIDFILE"
        echo "Stopped."
    else
        echo "dbhub not running."
    fi
}

do_start() {
    if _pid >/dev/null 2>&1; then
        echo "dbhub already running (PID $(_pid)). Use 'restart' to restart."
        do_status
        return 0
    fi

    if [ ! -f "$NODE" ]; then
        echo "ERROR: Node.js v24 not found at $NODE" >&2
        exit 1
    fi
    NODE_VERSION=$("$NODE" --version 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')
    if [ -z "$NODE_VERSION" ] || [ "$NODE_VERSION" -lt 24 ] 2>/dev/null; then
        echo "ERROR: Node.js >= 24 required (found: $("$NODE" --version 2>/dev/null || echo 'none'))" >&2
        echo "  Install via nvm: nvm install 24 && nvm use 24" >&2
        exit 1
    fi
    if [ ! -f "$DBHUB_ENTRY" ]; then
        echo "ERROR: dbhub not found at $DBHUB_ENTRY" >&2
        echo "  Install: $NODE $(dirname $NODE)/npm install -g @bytebase/dbhub@latest" >&2
        exit 1
    fi
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: config not found at $CONFIG" >&2
        exit 1
    fi

    echo "Starting dbhub HTTP on port $PORT..."
    cd "$PROJ"
    nohup "$NODE" "$DBHUB_ENTRY" \
        --transport http --port "$PORT" \
        --config "$CONFIG" \
        >> "$LOGFILE" 2>&1 &

    echo $! > "$PIDFILE"
    sleep 2

    if _pid >/dev/null 2>&1; then
        local ip bind
        ip=$(hostname -I | awk '{print $1}')
        bind=$(ss -tlnp 2>/dev/null | grep ":${PORT} " | awk '{print $4}' | head -1)
        echo "dbhub HTTP started (PID $(cat "$PIDFILE"))"
        echo ""
        if echo "$bind" | grep -q "0.0.0.0"; then
            echo "  Local:   http://localhost:$PORT/mcp"
            echo "  Network: http://$ip:$PORT/mcp"
            echo "  Log:     $LOGFILE"
            echo ""
            echo "Cursor mcp.json config for teammates:"
            echo ""
            echo "  \"signals-db\": {"
            echo "    \"url\": \"http://$ip:$PORT/mcp\""
            echo "  }"
        else
            echo "  Local:   http://localhost:$PORT/mcp"
            echo "  Log:     $LOGFILE"
            echo ""
            echo "  (Listening on localhost only. To expose to LAN, restart with --host 0.0.0.0)"
        fi
    else
        echo "ERROR: dbhub failed to start. Check $LOGFILE" >&2
        cat "$LOGFILE" | tail -20 >&2
        exit 1
    fi
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    status)  do_status ;;
    restart) do_stop; do_start ;;
    *)       echo "Usage: $0 {start|stop|status|restart}" >&2; exit 1 ;;
esac
