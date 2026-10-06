#!/usr/bin/env bash
cd /opt/ai-trader || exit 1
exec python3 /opt/ai-trader/beat.py "${1:-80}"
