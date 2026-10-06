#!/usr/bin/env bash
# 清空模拟盘（持仓 / 成交 / 曲线全部归零，现金重置为 INITIAL_CASH）。
# 仅在你确认要重新开始时运行。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

read -r -p "这会清空全部模拟盘记录，确定？输入 RESET 继续： " ans
[[ "$ans" == "RESET" ]] || { echo "已取消"; exit 0; }

set -a; source ./.env; set +a
rm -f data/db/trader.db data/db/trader.db-wal data/db/trader.db-shm
docker compose restart mcp-paper
sleep 3
echo "模拟盘已重置，初始资金 ${INITIAL_CASH:-10000}"
