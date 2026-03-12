#!/bin/bash
set -a
source /home/jfischer/claude/env.sh
set +a
exec /home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/dispatcher/dispatcher-server.py
