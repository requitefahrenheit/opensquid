#!/bin/bash
# Setup script for voice-wake daemon
set -e

cd ~/claude/opensquid/voice-wake

# Create virtual environment (use miniconda Python 3.12)
PYTHON=/home/jfischer/miniconda3/envs/agent/bin/python3
$PYTHON -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    pvporcupine \
    sounddevice \
    httpx \
    python-dotenv \
    numpy

echo "Setup complete. Activate with: source ~/claude/opensquid/voice-wake/venv/bin/activate"
