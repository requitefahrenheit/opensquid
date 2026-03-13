#!/bin/bash
# Setup script for daemon-server
set -e

cd ~/claude/opensquid/daemon

# Create virtual environment (use miniconda Python 3.12)
PYTHON=/home/jfischer/miniconda3/envs/agent/bin/python3
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
