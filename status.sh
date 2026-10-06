#!/usr/bin/env bash
# 查看 harness 当前状态。用法： bash status.sh
#                              bash beat.sh [行数]   看最近一轮心跳的完整叙事
cd /opt/ai-trader || exit 1
exec python3 /opt/ai-trader/status.py
