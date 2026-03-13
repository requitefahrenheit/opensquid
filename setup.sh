#!/bin/bash
set -e
cd ~/claude/opensquid

# Root venv for browser-server and channels-server
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install anthropic fastmcp uvicorn starlette httpx apscheduler \
    beautifulsoup4 python-dotenv pyyaml python-telegram-bot

deactivate

# Daemon has its own venv
bash daemon/setup.sh

echo "Setup complete. Run kick-off.sh to start services."
