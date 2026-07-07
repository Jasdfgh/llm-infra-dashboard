#!/usr/bin/env bash
# =============================================================================
# Start dbhub as an HTTP MCP server for internal network access.
#
# Usage:
#   bash scripts/start_dbhub_server.sh              # start (default port 8081)
#   bash scripts/start_dbhub_server.sh stop         # stop
#   bash scripts/start_dbhub_server.sh status       # check if running
#   bash scripts/start_dbhub_server.sh restart      # stop + start
#   bash scripts/start_dbhub_server.sh foreground   # run in foreground (for systemd)
#   PORT=9090 bash scripts/start_dbhub_server.sh    # custom port
# =============================================================================

set -euo pipefail

WORKSHOP="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$WORKSHOP/src" ] && [ -d "$(dirname "$WORKSHOP")/data" ]; then
    ROOT="$(dirname "$WORKSHOP")"
else
    ROOT="$WORKSHOP"
fi
NODE="${NODE:-$(which node 2>/dev/null || echo "node")}"
if [ -z "${DBHUB_ENTRY:-}" ]; then
    case "$NODE" in
        */bin/node)
            local_prefix="${NODE%/bin/node}"
            DBHUB_ENTRY="${local_prefix}/lib/node_modules/@bytebase/dbhub/dist/index.js"
            ;;
        *)
            node_dir="$(dirname "$NODE")"
            if [ -x "$node_dir/npm" ]; then
                DBHUB_ENTRY="$("$node_dir/npm" root -g 2>/dev/null)/@bytebase/dbhub/dist/index.js"
            elif command -v npm >/dev/null 2>&1; then
                DBHUB_ENTRY="$(npm root -g 2>/dev/null)/@bytebase/dbhub/dist/index.js"
            else
                echo "ERROR: Cannot locate dbhub. Set DBHUB_ENTRY=/path/to/dbhub/dist/index.js" >&2
                exit 1
            fi
            ;;
    esac
fi
CONFIG="$WORKSHOP/dbhub.toml"
PORT="${PORT:-8081}"
LOGFILE="$ROOT/data/dbhub_server.log"
PIDFILE="$ROOT/data/dbhub_server.pid"

mkdir -p "$ROOT/data"

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

_check_node_version() {
    local ver
    ver=$("$NODE" --version 2>/dev/null | sed 's/^v//')
    local major="${ver%%.*}"
    if [ -z "$major" ] || [ "$major" -lt 24 ] 2>/dev/null; then
        echo "ERROR: Node.js v24+ required, found: ${ver:-not found}" >&2
        echo "  Set NODE=/path/to/node24 or install via nvm" >&2
        return 1
    fi
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
    fi

    if systemctl --user is-active signals-dbhub.service >/dev/null 2>&1; then
        echo "dbhub HTTP running via systemd (port $PORT)"
        echo "  endpoint: http://localhost:$PORT/mcp"
        echo "  log: journalctl --user -u signals-dbhub"
        return 0
    fi

    if command -v ss >/dev/null 2>&1 && ss -tlnp 2>/dev/null | grep -q ":${PORT}[[:space:]]"; then
        echo "dbhub HTTP running on port $PORT (unknown manager)"
        return 0
    fi

    echo "dbhub HTTP not running"
    return 1
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

_ensure_config() {
    local TOML_EXAMPLE="$WORKSHOP/dbhub.toml.example"
    if [ -f "$TOML_EXAMPLE" ]; then
        if [ ! -f "$CONFIG" ] || [ "$TOML_EXAMPLE" -nt "$CONFIG" ] || \
           grep -Fq '__PROJECT_ROOT__' "$CONFIG" 2>/dev/null || \
           ! grep -Fq "$ROOT" "$CONFIG" 2>/dev/null; then
            sed "s|__PROJECT_ROOT__|$ROOT|g" "$TOML_EXAMPLE" > "$CONFIG"
            echo "Generated $CONFIG from template (root=$ROOT)"
        fi
    fi
}

do_foreground() {
    _ensure_config

    if [ ! -x "$NODE" ] && ! command -v "$NODE" >/dev/null 2>&1; then
        echo "ERROR: Node.js not found at $NODE" >&2
        exit 1
    fi
    _check_node_version || exit 1
    if [ ! -f "$DBHUB_ENTRY" ]; then
        echo "ERROR: dbhub not found at $DBHUB_ENTRY" >&2
        exit 1
    fi
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: config not found at $CONFIG" >&2
        exit 1
    fi

    echo "Starting dbhub in foreground (port=$PORT, config=$CONFIG)"
    exec "$NODE" "$DBHUB_ENTRY" \
        --transport http --port "$PORT" \
        --config "$CONFIG"
}

do_start() {
    if _pid >/dev/null 2>&1; then
        echo "dbhub already running (PID $(_pid)). Use 'restart' to restart."
        do_status
        return 0
    fi

    _ensure_config

    if [ ! -x "$NODE" ] && ! command -v "$NODE" >/dev/null 2>&1; then
        echo "ERROR: Node.js not found at $NODE" >&2
        exit 1
    fi
    _check_node_version || exit 1
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
    cd "$ROOT"
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
            echo "  (Listening on localhost only. dbhub binds 0.0.0.0 by default; check node/dbhub config to restrict.)"
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
    foreground|--foreground) do_foreground ;;
    *)       echo "Usage: $0 {start|stop|status|restart|foreground}" >&2; exit 1 ;;
esac
