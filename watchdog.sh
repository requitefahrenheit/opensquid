#!/bin/bash
# watchdog.sh — Supervise OpenSquid services, restart if down
# Usage: */5 * * * * bash ~/claude/opensquid/watchdog.sh >> ~/claude/opensquid/watchdog.log 2>&1

TS=$(date '+%Y-%m-%d %H:%M:%S')

check_and_restart() {
  local name=$1
  local port=$2
  local cmd=$3

  if ! (echo >/dev/tcp/localhost/${port}) 2>/dev/null; then
    echo "[$TS] $name (port $port) DOWN — restarting"
    eval "bash -c '$cmd'" &
    echo "[$TS] $name launched"
  fi
}

# Daemon (8256)
check_and_restart "opensquid-daemon" 8256 \
  "/home/jfischer/claude/opensquid/daemon/kick-off.sh >> /home/jfischer/claude/opensquid/daemon/daemon.log 2>&1"

# Browser (8258)
check_and_restart "opensquid-browser" 8258 \
  "/home/jfischer/claude/opensquid/browser/kick-off.sh >> /home/jfischer/claude/opensquid/browser/browser.log 2>&1"

# Channels (8257) — only if TELEGRAM_BOT_TOKEN is set
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  check_and_restart "opensquid-channels" 8257 \
    "/home/jfischer/claude/opensquid/channels/kick-off.sh >> /home/jfischer/claude/opensquid/channels/channels.log 2>&1"
fi
