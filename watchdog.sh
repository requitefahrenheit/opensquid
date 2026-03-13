#!/bin/bash
# watchdog.sh — Supervise OpenSquid services, restart if down
# Usage: */5 * * * * bash ~/claude/opensquid/watchdog.sh >> ~/claude/opensquid/watchdog.log 2>&1

# Load platform config
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

OPENSQUID_ROOT="${OPENSQUID_ROOT:-$HOME/claude/opensquid}"
TS=$(date '+%Y-%m-%d %H:%M:%S')

check_and_restart() {
  local name=$1
  local port=$2
  local cmd=$3

  local down=false
  if command -v lsof &>/dev/null; then
    lsof -ti:${port} &>/dev/null || down=true
  elif ! (echo >/dev/tcp/localhost/${port}) 2>/dev/null; then
    down=true
  fi

  if $down; then
    echo "[$TS] $name (port $port) DOWN — restarting"
    eval "bash -c '$cmd'" &
    echo "[$TS] $name launched"
  fi
}

# Daemon (8256)
check_and_restart "opensquid-daemon" 8256 \
  "$OPENSQUID_ROOT/daemon/kick-off.sh >> $OPENSQUID_ROOT/daemon/daemon.log 2>&1"

# Browser (8258)
check_and_restart "opensquid-browser" 8258 \
  "$OPENSQUID_ROOT/browser/kick-off.sh >> $OPENSQUID_ROOT/browser/browser.log 2>&1"

# Channels (8257) — only if TELEGRAM_BOT_TOKEN is set
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  check_and_restart "opensquid-channels" 8257 \
    "$OPENSQUID_ROOT/channels/kick-off.sh >> $OPENSQUID_ROOT/channels/channels.log 2>&1"
fi
