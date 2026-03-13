#!/bin/bash
# Kill old process, activate venv, start daemon-server
set -e

export OPENSQUID_ROOT="/home/jfischer/claude/opensquid"

cd ~/claude/opensquid/daemon

# Kill existing process on port 8256
pid=$(ss -tlnp 2>/dev/null | awk '/:8256 /{match($0, /pid=([0-9]+)/, m); if (m[1]) print m[1]}')
if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
sleep 1

# Activate venv
source ~/claude/opensquid/daemon/venv/bin/activate

# Start server
nohup python daemon-server.py > daemon.log 2>&1 &
echo "daemon-server started (PID $!), logging to daemon.log"
