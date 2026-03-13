#!/bin/bash
# Kill old process, activate venv, start browser-server
set -e

cd ~/claude/opensquid/browser

# Kill existing process on port 8258
pid=$(netstat -tlnp 2>/dev/null | awk '/:8258 /{match($0, /([0-9]+)\//, m); if (m[1]) print m[1]}')
if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
sleep 1

# Activate venv
source ~/claude/opensquid/browser/venv/bin/activate

# Start server
nohup python browser-server.py > browser.log 2>&1 &
echo "browser-server started (PID $!), logging to browser.log"
