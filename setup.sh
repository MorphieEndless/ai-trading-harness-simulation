#!/usr/bin/env bash
# AI Trading Harness 一键部署 / 更新脚本
# 用法：  bash setup.sh          # 首次部署或更新
#         bash setup.sh --no-nginx   # 只重建容器，不动 nginx
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DOMAIN="${DOMAIN:-trader.example.com}"
NGINX_CONF="/etc/nginx/conf.d/${DOMAIN}.conf"
HTPASSWD="/etc/nginx/.htpasswd-halluwithcrypto"

c()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
er() { printf '\033[1;31m  !! \033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------- 1. 配置文件
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  er "已从 .env.example 生成 .env —— 请先填好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
  er "以及 WEBUI_PASSWORD，然后重新运行本脚本。"
  exit 1
fi
chmod 600 .env
set -a; source ./.env; set +a

if [[ -z "${WEBUI_PASSWORD:-}" || "$WEBUI_PASSWORD" == "CHANGE_ME_STRONG_PASSWORD" ]]; then
  er ".env 里的 WEBUI_PASSWORD 还是默认值，请改成强密码后再运行。"
  exit 1
fi

# ---------------------------------------------------------------- 2. 目录与权限
c "准备数据目录"
mkdir -p data/db data/workspace/notes data/logs config
# paper 容器以 10001 运行，需要能写 db；brain 以 10001 运行，需要能写工作区
chown -R 10001:10001 data/db data/workspace
# 日志目录里的旧文件保持可写
chown -R 10001:10001 data/logs
chmod 750 data/db data/workspace data/logs
ok "data/{db,workspace,logs} 就绪（属主 10001）"

# config/ 是人类的地盘，含密钥 —— 锁紧，只给 root
chmod 755 config
if [[ ! -f config/subagent_models.json ]]; then
  cp config/subagent_models.example.json config/subagent_models.json
  ok "已生成 config/subagent_models.json（子代理白名单，人类维护）"
fi
if [[ ! -f config/persona.md ]]; then
  # 默认留空 = 不加人格，Agent 用 prompts.py 里的默认性格
  : > config/persona.md
  ok "已创建空的 config/persona.md（想加人格就编辑它，可参考 persona.example.md）"
fi
# config/ 里【不含密钥】，所以可以公开可读（Brain 以 UID 10001 运行需要读它）。
# 密钥在 .env 里，权限 600。
chmod 644 config/*.json config/*.md 2>/dev/null || true
ok "config/ 就绪（里面不含密钥，密钥在 .env）"

# ---------------------------------------------------------------- 3. 构建与启动
c "构建镜像（首次会拉取 python:3.12-slim，约 1-3 分钟）"
docker compose build

c "启动容器"
docker compose up -d --remove-orphans

c "等待服务就绪"
for i in $(seq 1 40); do
  if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:18080/api/health" \
      -u "${WEBUI_USER:-trader}:${WEBUI_PASSWORD}" 2>/dev/null; then
    ok "brain 已就绪"
    break
  fi
  sleep 2
  [[ $i -eq 40 ]] && er "brain 健康检查超时，请看 docker compose logs brain"
done

# ---------------------------------------------------------------- 4. nginx
if [[ "${1:-}" != "--no-nginx" ]]; then
  c "配置 nginx"
  if ! grep -q "zone=hallu " /etc/nginx/nginx.conf 2>/dev/null; then
    # 在 http{} 内补一个 limit_req 区（仅在缺失时插入一次）
    cp /etc/nginx/nginx.conf "/etc/nginx/nginx.conf.bak.$(date +%Y%m%d-%H%M%S)"
    awk '
      /^http[[:space:]]*\{/ && !done { print; print "    limit_req_zone $binary_remote_addr zone=hallu:10m rate=10r/s;"; done=1; next }
      { print }
    ' /etc/nginx/nginx.conf > /etc/nginx/nginx.conf.new
    mv /etc/nginx/nginx.conf.new /etc/nginx/nginx.conf
    ok "已在 nginx.conf 注册 limit_req 区 hallu"
  else
    ok "limit_req 区已存在"
  fi

  # 同套凭证生成 htpasswd（openssl 已在系统里，不依赖 apache2-utils）
  OPENSSL_BIN="$(command -v openssl)"
  printf '%s:%s\n' "${WEBUI_USER:-trader}" \
    "$("$OPENSSL_BIN" passwd -apr1 "$WEBUI_PASSWORD")" > "$HTPASSWD"
  chmod 640 "$HTPASSWD"
  chown root:www-data "$HTPASSWD" 2>/dev/null || true
  ok "已写入 $HTPASSWD"

  install -m 644 "nginx/${DOMAIN}.conf" "$NGINX_CONF"
  ok "已安装 $NGINX_CONF"

  if nginx -t 2>&1 | tail -3; then
    systemctl reload nginx
    ok "nginx 已重载"
  else
    er "nginx 配置校验失败，未重载。请检查 $NGINX_CONF"
    exit 1
  fi
fi

# ---------------------------------------------------------------- 5. 汇总
echo
c "部署完成"
docker compose ps
echo
echo "  面板地址 : https://${DOMAIN}/"
echo "  登录凭证 : ${WEBUI_USER:-trader} / (见 .env 的 WEBUI_PASSWORD)"
echo "  查看日志 : docker compose logs -f brain"
echo "  查看状态 : bash status.sh"
echo "  看某轮心跳: bash beat.sh"
echo "  停止     : docker compose down"
echo
echo "  人格文件 : config/persona.md        （改完立即生效，不用重启）"
echo "  子代理   : config/subagent_models.json （模型白名单，人类维护）"
echo
if grep -q "REPLACE_ME" .env; then
  er "注意：.env 里的 LLM_API_KEY 仍是占位符，Agent 还不会交易。"
  er "     填好后执行： bash set-llm.sh <base_url> <api_key> <model>"
fi
