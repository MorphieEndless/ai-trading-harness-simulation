#!/usr/bin/env bash
# 跑 brain 的测试。用法：
#   bash runtests.sh nudge     # 纯逻辑，不联网不花钱：运行时成本提醒的触发口径
#   bash runtests.sh narrator  # 纯逻辑，不联网不花钱：叙事层的单向性 / 预算 / 触发口径
#   bash runtests.sh wake      # 纯逻辑，不联网不花钱：唤醒策略 / 睡眠配额 / 各类钳位
#   bash runtests.sh frontend  # 纯静态（不需要 docker）：面板 JS 结构 + DOM id 对账
#   bash runtests.sh web       # 纯逻辑，不联网：/api/market 不许被慢取数挡住
#   bash runtests.sh public    # 纯逻辑 + 真打 ASGI：匿名只读档的边界（谁能碰到什么）
#   bash runtests.sh hub       # 纯逻辑，不联网不花钱：MCP 多块返回值的装配
#   bash runtests.sh sandbox   # 纯逻辑 + 真挂规则：run_shell 的 Landlock 白名单
#   bash runtests.sh prompts   # 纯静态：操盘提示词的不变量（重写它时的安全网）
#   bash runtests.sh lint      # 纯静态，不联网不花钱：抓"用了但没定义的变量"
#   bash runtests.sh cap       # 真并发，会花 token：一次发 5 个 delegate，看峰值会不会超过上限
#   bash runtests.sh all       # 全跑#
# nudge / narrator / wake / hub / sandbox / lint / frontend / web / public 考的都是**当前源码**（改完立刻验，不用重建镜像）；
# cap 考的是**镜像里已部署的那份**（并发是运行时行为，必须验线上的）。
# 沙箱还有一份部署后的验收：python3 test_sandbox_live.py（打容器里正在跑的那份）。
# 睡眠账本同理：python3 test_paper_sleep.py（在一次性 DB 上打真代码，不碰真账本）。
#
# 两个坑（都踩过，别再踩）：
#   1. 镜像里 /app/trader 是构建时烤进去的。测试脚本若放在 /app 下，
#      sys.path[0] 会命中那份**旧代码** —— 表现出来就是"测了半天，
#      发现跑的还是改动前的默认值"。所以纯逻辑测试要先把源码拷到别的目录。
#   2. /opt/ai-trader 是 700，容器里的非 root 用户（UID 10001）挂不进去，
#      挂载点会变成"目录不存在"。所以统一拷一份到 /tmp 再挂。
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-all}"
IMAGE=ai-trader/brain:latest
STAGE=/tmp/trader-test

stage_src() {   # 源码 + 测试脚本 → 容器读得到的目录
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -r brain/trader "$STAGE/trader"
  # static/ 也要拷：test_public.py 会真去供 live.html 和 index.html，
  # 不拷的话它测到的只是一段 if os.path.isfile 的路径拼接。
  cp -r brain/static "$STAGE/static" 2>/dev/null || true
  # nginx/ 同理：test_public.py 的 [10] 会解析那份 conf、跟路由白名单对账。
  # 不拷的话它会打印「⚠ 跳过」—— 而**跳过不算通过**（2026-09-27 第一次在服务器上
  # 跑 public 时才发现的：本机跑得到 nginx/，容器里跑不到，于是那一节永远绿不了也红不了）。
  cp -r nginx "$STAGE/nginx" 2>/dev/null || true
  cp test_nudge.py "$STAGE/" 2>/dev/null || true
  cp test_narrator.py "$STAGE/" 2>/dev/null || true
  cp test_lint.py "$STAGE/" 2>/dev/null || true
  cp test_wake.py "$STAGE/" 2>/dev/null || true
  cp test_hub.py "$STAGE/" 2>/dev/null || true
  cp test_sandbox.py "$STAGE/" 2>/dev/null || true
  cp test_prompts.py "$STAGE/" 2>/dev/null || true
  cp test_web.py "$STAGE/" 2>/dev/null || true
  cp test_public.py "$STAGE/" 2>/dev/null || true
  chmod -R a+rX "$STAGE"
}

run_nudge() {   # 不需要网络、账本，也不花任何 token
  stage_src
  echo "=== test_nudge.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_nudge.py
}

run_narrator() {  # 同上：纯逻辑。考单向性 / 预算 / 触发口径
  stage_src
  echo
  echo "=== test_narrator.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_narrator.py
}

run_wake() {    # 同上：纯逻辑。考唤醒口径 / 睡眠配额 / 各类钳位
  stage_src
  echo
  echo "=== test_wake.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_wake.py
}

run_hub() {       # 纯逻辑：MCP 多块返回值装配（会静默吞数据的那类坑）
  stage_src
  echo
  echo "=== test_hub.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_hub.py
}

run_sandbox() {   # 纯逻辑 + 真挂规则：run_shell 的白名单 / ABI 降级 / 环境变量
  stage_src
  echo
  echo "=== test_sandbox.py（当前源码，不联网）==="
  # 注意：Landlock 是内核行为，容器和宿主是同一个内核，所以这里挂得上的规则
  # 和线上是同一套。本机内核不支持时会打印"跳过"而不是"通过"。
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_sandbox.py
}

run_prompts() {   # 纯逻辑 + 纯静态：操盘提示词的不变量（重写它的时候用这个兜底）
  stage_src
  echo
  echo "=== test_prompts.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_prompts.py
}

run_frontend() {   # 纯静态，连 docker 都不用：面板 JS 结构 + DOM id 对账
  echo
  echo "=== test_frontend.py（纯 Python，不需要容器）==="
  python3 test_frontend.py
}

run_web() {       # 纯逻辑 + 真打 ASGI：/api/market 愿不愿意等那个慢取数
  stage_src
  echo
  echo "=== test_web.py（当前源码，不联网）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_web.py
}

run_public() {    # 纯逻辑 + 真打 ASGI：匿名能碰到什么（路由白名单 / 脱敏 / 并发闸门）
  stage_src
  echo
  echo "=== test_public.py（当前源码，不联网）==="
  # 它把 app.routes 全枚举一遍，逐条匿名打过去：除 PUBLIC_ROUTES 外必须 401。
  # 加路由忘了想"这要不要给游客"的时候，红的就是这里。
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_public.py
}

run_lint() {      # 纯静态：不需要网络，也不执行任何东西
  stage_src
  echo
  echo "=== test_lint.py（当前源码，纯静态）==="
  docker run --rm -v "$STAGE":/t:ro --entrypoint python "$IMAGE" /t/test_lint.py
}

run_cap() {     # 要连 MCP 容器，所以挂进 trader 网络；考的是**镜像里**的代码
  echo
  echo "=== test_cap.py（部署在镜像里的代码，会花 token）==="
  # 注意挂在 /app/test_cap.py：sys.path[0] 才会是 /app，
  # 从而 import 到镜像里那份 trader 包 —— 这才是"线上正在跑的代码"。
  docker run --rm --network ai-trader_trader --env-file .env \
    -e MARKET_MCP_URL=http://mcp-market:8081/mcp \
    -e PAPER_MCP_URL=http://mcp-paper:8082/mcp \
    -e SUBAGENT_MODELS_FILE=/app/config/subagent_models.json \
    -e WORKSPACE_DIR=/tmp/ws -e LOG_DIR=/tmp \
    -v "$PWD/test_cap.py":/app/test_cap.py:ro \
    -v "$PWD/config":/app/config:ro \
    --entrypoint python "$IMAGE" /app/test_cap.py
}

case "$MODE" in
  nudge)    run_nudge ;;
  narrator) run_narrator ;;
  wake)     run_wake ;;
  hub)      run_hub ;;
  sandbox)  run_sandbox ;;
  prompts)  run_prompts ;;
  frontend) run_frontend ;;
  web)      run_web ;;
  public)   run_public ;;
  lint)     run_lint ;;
  cap)      run_cap ;;
  live)     python3 test_sandbox_live.py ;;
  paper)    python3 test_paper_sleep.py ;;
  all)      run_nudge; run_narrator; run_wake; run_hub; run_sandbox; run_prompts; run_frontend; run_web; run_public; run_lint; run_cap ;;
  *)        echo "用法: bash runtests.sh [nudge|narrator|wake|hub|sandbox|prompts|frontend|web|public|lint|cap|live|paper|all]"; exit 2 ;;
esac
