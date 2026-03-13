#!/bin/bash
# Kill old process, activate venv, start daemon-server
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

OPENSQUID_ROOT="${OPENSQUID_ROOT:-$SCRIPT_DIR/..}"
cd "$SCRIPT_DIR"

# Kill existing process on port 8256 (Linux: netstat, Mac: lsof)
if command -v lsof &>/dev/null; then
  pid=$(lsof -ti:8256 2>/dev/null)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
elif command -v netstat &>/dev/null; then
  pid=$(netstat -tlnp 2>/dev/null | awk '/:8256 /{match($0, /([0-9]+)\//, m); if (m[1]) print m[1]}')
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
fi
sleep 1

# Activate venv and start
source "$SCRIPT_DIR/venv/bin/activate"
nohup python daemon-server.py > "$SCRIPT_DIR/daemon.log" 2>&1 &
echo "daemon-server started (PID $!), logging to $SCRIPT_DIR/daemon.log"
