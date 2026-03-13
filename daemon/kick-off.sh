#!/bin/bash
# Kill old process, activate venv, start daemon-server
set -e

export OPENSQUID_ROOT="/home/jfischer/claude/opensquid"

cd ~/claude/opensquid/daemon

# Kill existing process on port 8256
fuser -k 8256/tcp 2>/dev/null || true
sleep 1

# Activate venv
source ~/claude/opensquid/daemon/venv/bin/activate

# Start server
nohup python daemon-server.py > daemon.log 2>&1 &
echo "daemon-server started (PID $!), logging to daemon.log"
