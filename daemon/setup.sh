#!/bin/bash
# Setup script for daemon-server
set -e

cd ~/claude/opensquid/daemon

# Create virtual environment (use miniconda Python 3.12)
ENV_FILE="$(dirname "$0")/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a
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
