#!/bin/bash
# Kill old process, activate venv, start daemon-server
set -e

# Load platform config
ENV_FILE="$(dirname "$0")/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

OPENSQUID_ROOT="${OPENSQUID_ROOT:-$HOME/claude/opensquid}"
cd "$OPENSQUID_ROOT/daemon"

# Kill existing process on port 8256 (Linux: netstat, Mac: lsof)
if command -v netstat &>/dev/null && netstat -tlnp 2>/dev/null | grep -q ':8256 '; then
  pid=$(netstat -tlnp 2>/dev/null | awk '/:8256 /{match($0, /([0-9]+)\//, m); if (m[1]) print m[1]}')
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
elif command -v lsof &>/dev/null; then
  pid=$(lsof -ti:8256 2>/dev/null)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
fi
sleep 1

# Activate venv and start
source "$OPENSQUID_ROOT/daemon/venv/bin/activate"
nohup python daemon-server.py > daemon.log 2>&1 &
echo "daemon-server started (PID $!), logging to daemon.log"
