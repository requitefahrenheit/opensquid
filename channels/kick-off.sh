#!/bin/bash
# Kill old process, activate venv, start channels-server
set -e

cd ~/claude/opensquid/channels

# Kill existing process on port 8257
pid=$(ss -tlnp 2>/dev/null | awk '/:8257 /{match($0, /pid=([0-9]+)/, m); if (m[1]) print m[1]}')
if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
sleep 1

# Activate venv
source ~/claude/opensquid/channels/venv/bin/activate

# Start server
nohup python channels-server.py > channels.log 2>&1 &
echo "channels-server started (PID $!), logging to channels.log"
