#!/bin/bash
# kick-off.sh — Kill and restart rwx-server + shared cloudflare tunnel
source ~/claude/env.sh
set -e

PORT=8251
TUNNEL_ID=5382c123-1ceb-4b5f-9200-94974a8f6ee9
SERVER=~/claude/rwx/rwx-server.py
LOG_DIR=~/claude/rwx/logs
mkdir -p "$LOG_DIR"

# --- rwx-server ---
echo "🔄 Stopping existing rwx-server..."
pkill -f "python3.*rwx-server" 2>/dev/null && echo "  Killed rwx-server" || echo "  No rwx-server running"
sleep 1

echo "🚀 Starting rwx-server on port ${PORT}..."
nohup /home/jfischer/miniconda3/envs/agent/bin/python3 "$SERVER" > "$LOG_DIR/server.log" 2>&1 &
RWX_PID=$!
echo "  PID: $RWX_PID"

# --- shared cloudflared tunnel ---
echo "🔄 Stopping existing shared tunnel..."
pkill -f "cloudflared tunnel run $TUNNEL_ID" 2>/dev/null && echo "  Killed cloudflared" || echo "  No tunnel running"
sleep 1

echo "🌐 Starting shared cloudflared tunnel..."
nohup ~/cloudflared tunnel run $TUNNEL_ID > "$LOG_DIR/tunnel.log" 2>&1 &
TUNNEL_PID=$!
echo "  PID: $TUNNEL_PID"

sleep 2

# --- health check ---
echo ""
if kill -0 $RWX_PID 2>/dev/null && kill -0 $TUNNEL_PID 2>/dev/null; then
    echo "═══════════════════════════════════════════"
    echo "  ✅ rwx-server running (PID $RWX_PID)"
    echo "  ✅ cloudflared tunnel running (PID $TUNNEL_PID)"
    echo ""
    echo "  MCP URL: https://rwx.fahrenheitrequited.dev/mcp?token=emc2ymmv"
    echo "═══════════════════════════════════════════"
else
    echo "❌ Something failed. Check $LOG_DIR/"
    tail -5 "$LOG_DIR/server.log"
    tail -5 "$LOG_DIR/tunnel.log"
fi
