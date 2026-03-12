#!/bin/bash
# watchdog.sh — Check all services and restart if down
# Cron: */5 * * * * bash ~/claude/watchdog.sh >> ~/claude/watchdog.log 2>&1

source ~/claude/env.sh
TS=$(date '+%Y-%m-%d %H:%M:%S')

check_and_restart() {
  local name=$1
  local port=$2
  local cmd=$3

  if ! netstat -tlnp 2>/dev/null | grep -q ":${port} " && ! (echo >/dev/tcp/localhost/${port}) 2>/dev/null; then
    echo "[$TS] $name (port $port) DOWN — restarting"
    eval "bash -c '$cmd' &" && disown $!
    echo "[$TS] $name launched (PID $!)"
  fi
}

# rwx-server (8251) — if down, run full kick-off to also restore tunnel
if ! ss -tlnp | grep -q ":8251 "; then
  echo "[$TS] rwx-server DOWN — running kick-off"
  bash /home/jfischer/claude/rwx/kick-off.sh
fi

# Open Mind (8250)
check_and_restart "om-server" 8250 \
  "/home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/_open-mind/om-server.py >> /home/jfischer/claude/_open-mind/openmind.log 2>&1"

# Open Mind MCP (8254)
check_and_restart "om-mcp" 8254 \
  "/home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/open-mind-mcp/om-mcp-server.py >> /home/jfischer/claude/open-mind-mcp/om-mcp.log 2>&1"

# Cortex jeremy (8080)
check_and_restart "cortex-jeremy" 8080 \
  "/home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/mcp-server/dual-server.py >> /home/jfischer/claude/mcp-server/cortex.log 2>&1"

# Cortex autonomous (8082)
check_and_restart "cortex-autonomous" 8082 \
  "CORTEX_DB=/home/jfischer/cortex/autonomous.db CORTEX_PORT=8082 /home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/mcp-server/dual-server.py >> /home/jfischer/claude/mcp-server/autonomous.log 2>&1"

# Therapy Finder (8252)
check_and_restart "therapy-finder" 8252 \
  "python3 /home/jfischer/claude/therapy-finder/therapy-finder-server.py >> /home/jfischer/claude/therapy-finder/logs/server.log 2>&1"

# Dispatcher (8255)
check_and_restart "dispatcher" 8255 \
  "/home/jfischer/claude/dispatcher/start.sh >> /home/jfischer/claude/dispatcher/dispatcher.log 2>&1"

# Open Photo (8260) — has its own cron but belt-and-suspenders
check_and_restart "op-server" 8260 \
  "cd /home/jfischer/claude/open-photo && /home/jfischer/miniconda3/envs/agent/bin/python3 -u op-server.py >> server.log 2>&1"
