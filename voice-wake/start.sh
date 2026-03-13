#!/bin/bash
# Start voice-wake daemon (no port — just nohup start)
set -e

ENV_FILE="$(dirname "$0")/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

OPENSQUID_ROOT="${OPENSQUID_ROOT:-$HOME/claude/opensquid}"
cd "$OPENSQUID_ROOT/voice-wake"

# Kill existing if running
pkill -f "voice-wake.py" 2>/dev/null || true
sleep 1

# Start daemon (using miniconda python — venv pip broken on this OS)
nohup ${OPENSQUID_PYTHON:-python3} voice-wake.py > voice-wake.log 2>&1 &
echo "voice-wake started (PID $!), logging to voice-wake.log"
