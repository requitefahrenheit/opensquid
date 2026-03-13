#!/bin/bash
# Setup script for browser-server
set -e

cd ~/claude/opensquid/browser

# Create virtual environment (use miniconda Python 3.12)
PYTHON=/home/jfischer/miniconda3/envs/agent/bin/python3
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
