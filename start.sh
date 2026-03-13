#!/bin/bash
# Simple start (foreground, no kill) — use kick-off.sh for clean restarts
cd ~/claude/opensquid/daemon
source venv/bin/activate
python3 daemon-server.py
