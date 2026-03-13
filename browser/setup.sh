#!/bin/bash
# Setup script for browser-server
set -e

cd ~/claude/opensquid/browser

# Create virtual environment (use miniconda Python 3.12)
ENV_FILE="$(dirname "$0")/../.env"
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a
PYTHON="${OPENSQUID_PYTHON:-$(which python3)}"
$PYTHON -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    httpx \
    beautifulsoup4 \
    fastmcp \
    uvicorn

echo "Setup complete. Activate with: source ~/claude/opensquid/browser/venv/bin/activate"
