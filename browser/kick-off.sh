#!/bin/bash
# Kill old process, activate venv, start browser-server
set -e

cd ~/claude/opensquid/browser

# Kill existing process on port 8258
fuser -k 8258/tcp 2>/dev/null || true
sleep 1

# Activate venv
source ~/claude/opensquid/browser/venv/bin/activate

# Start server
nohup python browser-server.py > browser.log 2>&1 &
echo "browser-server started (PID $!), logging to browser.log"
