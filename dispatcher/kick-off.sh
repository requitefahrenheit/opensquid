#!/bin/bash
# kick-off.sh — Start the dispatcher server

source ~/claude/env.sh

PYTHON=/home/jfischer/miniconda3/envs/agent/bin/python3
LOG=/home/jfischer/claude/dispatcher/dispatcher.log
SERVER=/home/jfischer/claude/dispatcher/dispatcher-server.py

echo "[$(date)] Starting dispatcher-server.py on port 8255" >> "$LOG"
nohup "$PYTHON" -u "$SERVER" >> "$LOG" 2>&1 &
echo "[$(date)] Dispatcher started (PID $!)" >> "$LOG"
