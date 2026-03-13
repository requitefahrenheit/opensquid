#!/bin/bash
# Kill old process, activate venv, start channels-server
set -e

cd ~/claude/opensquid/channels

# Kill existing process on port 8257
fuser -k 8257/tcp 2>/dev/null || true
sleep 1

# Activate venv
source ~/claude/opensquid/channels/venv/bin/activate

# Start server
nohup python channels-server.py > channels.log 2>&1 &
echo "channels-server started (PID $!), logging to channels.log"
