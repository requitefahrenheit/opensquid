#!/bin/bash
# Setup script for daemon-server
set -e

cd ~/claude/parity/daemon

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    fastmcp \
    anthropic \
    httpx \
    apscheduler \
    uvicorn

echo "Setup complete. Activate with: source ~/claude/parity/daemon/venv/bin/activate"