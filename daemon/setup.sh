#!/bin/bash
# Setup script for daemon-server
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a
cd "$SCRIPT_DIR"

# Create virtual environment
PYTHON="${OPENSQUID_PYTHON:-$(which python3)}"
$PYTHON -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    fastmcp \
    anthropic \
    httpx \
    apscheduler \
    uvicorn \
    starlette

echo "Setup complete. Run kick-off.sh to start daemon."
