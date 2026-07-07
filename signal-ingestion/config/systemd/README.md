# systemd Service Deployment Guide

## Overview

User-level systemd services manage the signal ingestion system:

| File | Purpose |
|------|---------|
| `signals-alert@.service.example` | OnFailure alert template — sends Teams notification via `notify_teams.sh` |
| `signals-sync-mcp.service.example` | MCP query/sync/management service (port 8082) |
| `signals-dbhub.service.example` | dbhub SQL MCP server (port 8081), started via `start_dbhub_server.sh` |
| `signals-sync.service.example` | Incremental GitHub sync (oneshot, called by timer) |
| `signals-sync.timer.example` | Runs `signals-sync.service` every 2 hours |
| `signals-dbhub-restart.service.example` | Planned daily restart of `signals-dbhub` (oneshot, called by timer) |
| `signals-dbhub-restart.timer.example` | Triggers `signals-dbhub-restart.service` daily at 04:00 |

Both `signals-sync-mcp` and `signals-dbhub` reference `OnFailure=signals-alert@%n.service`.
The dbhub service uses `start_dbhub_server.sh foreground` as its launcher, which runs the
node process directly (via `exec`) so systemd can supervise, restart, and enforce resource
limits on the real dbhub process. For manual CLI usage, `start_dbhub_server.sh start`
still launches in the background with PID tracking. The script auto-generates `dbhub.toml`
from `dbhub.toml.example` — no manual toml setup is needed on fresh deploy.

## Placeholders

`.example` files use placeholders that must be replaced at deploy time:

- `__PROJECT_ROOT__` — project root directory (e.g. `/home/user/my-llm-infra-dashboard`)
- `__NODE_PREFIX__` — Node.js v24 install prefix (e.g. `/home/user/.nvm/versions/node/v24.14.0`); used by `signals-dbhub.service.example` to set explicit `NODE` and `DBHUB_ENTRY` paths so systemd does not depend on ambient PATH or nvm

## Deployment Steps

```bash
# 1. Install the alert template FIRST (other services depend on it)
sed "s|__PROJECT_ROOT__|$(pwd)|g" config/systemd/signals-alert@.service.example > ~/.config/systemd/user/signals-alert@.service

# 2. Install the MCP service
sed "s|__PROJECT_ROOT__|$(pwd)|g" config/systemd/signals-sync-mcp.service.example > ~/.config/systemd/user/signals-sync-mcp.service

# 3. Install the dbhub service (replace both __PROJECT_ROOT__ and __NODE_PREFIX__)
NODE_PREFIX="/home/user/.nvm/versions/node/v24.14.0"   # adjust to your nvm path
sed -e "s|__PROJECT_ROOT__|$(pwd)|g" \
    -e "s|__NODE_PREFIX__|$NODE_PREFIX|g" \
    config/systemd/signals-dbhub.service.example > ~/.config/systemd/user/signals-dbhub.service

# 4. Reload and start
systemctl --user daemon-reload
systemctl --user restart signals-sync-mcp signals-dbhub

# 5. Verify OnFailure is set
systemctl --user show signals-sync-mcp -p OnFailure
systemctl --user show signals-dbhub -p OnFailure
# Both should output: OnFailure=signals-alert@%n.service
```

## Timer Deployment

```bash
# 5. Install the sync timer pair
sed "s|__PROJECT_ROOT__|$(pwd)|g" config/systemd/signals-sync.service.example > ~/.config/systemd/user/signals-sync.service
cp config/systemd/signals-sync.timer.example ~/.config/systemd/user/signals-sync.timer

# 6. Install the dbhub daily restart timer pair
cp config/systemd/signals-dbhub-restart.service.example ~/.config/systemd/user/signals-dbhub-restart.service
cp config/systemd/signals-dbhub-restart.timer.example ~/.config/systemd/user/signals-dbhub-restart.timer

# 7. Reload and enable timers
systemctl --user daemon-reload
systemctl --user enable --now signals-sync.timer signals-dbhub-restart.timer

# 8. Verify timers are active
systemctl --user list-timers 'signals-*'
```

## Verify Alerting

```bash
# Trigger a test alert (kill MCP, should receive Teams alert within 30s)
systemctl --user kill -s SIGKILL signals-sync-mcp
# Check alert template was triggered
journalctl --user -u 'signals-alert@*' --since '1 min ago'
# Restore service
systemctl --user start signals-sync-mcp
```

## Rollback

```bash
# Restore from backups (if you made them before deploying)
cp ~/.config/systemd/user/signals-sync-mcp.service.bak ~/.config/systemd/user/signals-sync-mcp.service
cp ~/.config/systemd/user/signals-dbhub.service.bak ~/.config/systemd/user/signals-dbhub.service
systemctl --user daemon-reload
systemctl --user restart signals-sync-mcp signals-dbhub
```
