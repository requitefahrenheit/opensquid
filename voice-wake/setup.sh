#!/bin/bash
# Setup script for voice-wake daemon
set -e

cd ~/claude/parity/voice-wake

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install \
    pvporcupine \
    sounddevice \
    httpx \
    python-dotenv \
    numpy

echo "Setup complete. Activate with: source ~/claude/parity/voice-wake/venv/bin/activate"
