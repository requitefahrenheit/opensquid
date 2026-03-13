#!/bin/bash
# Setup script for channels-server
set -e

cd ~/claude/parity/channels

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    python-telegram-bot \
    httpx \
    python-dotenv \
    uvicorn \
    starlette

echo "Setup complete. Activate with: source ~/claude/parity/channels/venv/bin/activate"
