#!/bin/bash
# Kill old processes, then start all opensquid services
OPENSQUID=~/claude/opensquid
LOG=$OPENSQUID/daemon/daemon.log

# Kill existing processes
fuser -k 8256/tcp 2>/dev/null || true
fuser -k 8257/tcp 2>/dev/null || true
fuser -k 8258/tcp 2>/dev/null || true
sleep 1

# Start daemon (primary service)
bash $OPENSQUID/daemon/kick-off.sh

# Start browser
bash $OPENSQUID/browser/kick-off.sh

echo "OpenSquid services started (daemon 8256, browser 8258)."
echo "Channels-server requires TELEGRAM_BOT_TOKEN — start manually:"
echo "  bash $OPENSQUID/channels/kick-off.sh"
