#!/usr/bin/env bash
# 换模型/换 key 的一行命令。用法：
#   bash set-llm.sh https://your-endpoint/v1 sk-xxxx your-model-name
# 注意：key 只会写进 .env（600 权限），不会出现在前端、日志或镜像里。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BASE_URL="${1:-}"; API_KEY="${2:-}"; MODEL="${3:-}"
if [[ -z "$BASE_URL" || -z "$API_KEY" || -z "$MODEL" ]]; then
  echo "用法: bash set-llm.sh <base_url> <api_key> <model>" >&2
  exit 1
fi

[[ -f .env ]] || { echo "缺少 .env，请先跑 setup.sh" >&2; exit 1; }

python3 - "$BASE_URL" "$API_KEY" "$MODEL" <<'PY'
import sys, re, pathlib
base, key, model = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(".env")
text = p.read_text(encoding="utf-8")
def setv(text, k, v):
    if re.search(rf'^{re.escape(k)}=.*$', text, flags=re.M):
        return re.sub(rf'^{re.escape(k)}=.*$', f'{k}={v}', text, flags=re.M)
    return text.rstrip("\n") + f"\n{k}={v}\n"
for k, v in (("LLM_BASE_URL", base), ("LLM_API_KEY", key), ("LLM_MODEL", model)):
    text = setv(text, k, v)
p.write_text(text, encoding="utf-8")
PY

chmod 600 .env
echo "已更新 .env 中的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
docker compose up -d --force-recreate brain
echo
echo "brain 已重启，模型配置生效。"
echo "验证： docker compose logs --tail=30 brain"
