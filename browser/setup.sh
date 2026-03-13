#!/bin/bash
# Setup script for browser-server
set -e

cd ~/claude/parity/browser

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    httpx \
    beautifulsoup4 \
    fastmcp \
    uvicorn

echo "Setup complete. Activate with: source ~/claude/parity/browser/venv/bin/activate"
