#!/bin/bash
# Start all opensquid services (each kick-off.sh handles its own port cleanup)
OPENSQUID=~/claude/opensquid

# Start daemon (primary service)
bash $OPENSQUID/daemon/kick-off.sh

# Start browser
bash $OPENSQUID/browser/kick-off.sh

echo "OpenSquid services started (daemon 8256, browser 8258)."
echo "Channels-server requires TELEGRAM_BOT_TOKEN — start manually:"
echo "  bash $OPENSQUID/channels/kick-off.sh"
