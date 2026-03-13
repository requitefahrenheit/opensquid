#!/bin/bash
# Start voice-wake daemon (no port — just nohup start)
set -e

cd ~/claude/parity/voice-wake

# Kill existing if running
pkill -f "voice-wake.py" 2>/dev/null || true
sleep 1

# Start daemon (using miniconda python — venv pip broken on this OS)
nohup /home/jfischer/miniconda3/envs/agent/bin/python3 voice-wake.py > voice-wake.log 2>&1 &
echo "voice-wake started (PID $!), logging to voice-wake.log"
