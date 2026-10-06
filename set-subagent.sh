#!/usr/bin/env bash
# 管理子代理模型白名单（config/subagent_models.json）。
#
# 用法：
#   bash set-subagent.sh --list                     查看当前白名单
#   bash set-subagent.sh --interim                  灌入三个实测可用的便宜模型
#   bash set-subagent.sh --add <id> <base_url> [api_key] [max_tokens]
#                                                  添加/更新一个模型
#   bash set-subagent.sh --remove <id>              移除一个模型
#   bash set-subagent.sh --default <id>             设置默认模型
#   bash set-subagent.sh --off                      清空白名单（delegate 不再暴露）
#
# 改完立即生效，不用重启（Agent 每次委派都会重新读这个文件）。
# 密钥只写在 config/ 里（700 权限目录 + 600 文件），不进前端、不进日志、不进镜像。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODELS_JSON="config/subagent_models.json"
EXAMPLE="config/subagent_models.example.json"

[[ -d config ]] || { echo "缺少 config 目录，请先跑 setup.sh" >&2; exit 1; }

# 首次使用时从示例播种
if [[ ! -f "$MODELS_JSON" ]]; then
  if [[ -f "$EXAMPLE" ]]; then
    cp "$EXAMPLE" "$MODELS_JSON"
    echo "已从示例生成 $MODELS_JSON"
  else
    printf '{"default":"","models":[]}\n' > "$MODELS_JSON"
    echo "已创建空的 $MODELS_JSON"
  fi
  chmod 644 "$MODELS_JSON"
fi

reload() {
  docker compose up -d --force-recreate brain >/dev/null 2>&1 || true
  sleep 7
  PW="$(grep '^WEBUI_PASSWORD=' .env | cut -d= -f2-)"
  curl -sS -m 20 -u "trader:${PW}" http://127.0.0.1:18080/api/health 2>/dev/null \
    | python3 -c '
import json,sys
try:
    s = json.load(sys.stdin)["agent"].get("subagent", {})
except Exception:
    print("(状态读取失败)"); raise SystemExit
if not s.get("enabled"):
    print("子代理：关闭（delegate 不会暴露给主 Agent）")
    raise SystemExit
print(f"子代理：启用  默认 {s.get(\"default_model\")}  并发上限 {s.get(\"max_concurrency\")}")
for m in s.get("models", []):
    print(f"  · {m[\"id\"]:<24} {m.get(\"label\") or \"\"}")
' 2>/dev/null || echo "(状态读取失败)"
}

MODE="${1:-}"; shift || true

case "$MODE" in
  --list)
    python3 -c '
import json, pathlib
d = json.loads(pathlib.Path("config/subagent_models.json").read_text(encoding="utf-8"))
ms = d.get("models") or []
print(f"默认: {d.get(\"default\") or \"(未设置)\"}   共 {len(ms)} 个")
for m in ms:
    print(f"\n  {m.get(\"id\")}   [{m.get(\"label\") or \"\"}]")
    print(f"    base_url : {m.get(\"base_url\") or \"(继承 LLM_BASE_URL)\"}")
    print(f"    api_key_env: {(chr(42)*8) if m.get(\"api_key\") else \"(回落环境变量)\"}")
    print(f"    适合     : {m.get(\"use_for\") or \"-\"}")
    print(f"    不适合   : {m.get(\"avoid_for\") or \"-\"}")
'
    ;;

  --interim)
    python3 - "$MODELS_JSON" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text(encoding="utf-8"))
base = ""
env = pathlib.Path(".env").read_text(encoding="utf-8")
for line in env.splitlines():
    if line.startswith("LLM_BASE_URL="):
        base = line.split("=", 1)[1].strip()
seeds = [
    ("glm-5.3-fast", "GLM 5.3 Fast",
     "最快最省的批量活儿。扫描多个标的做排名、把长数据序列读成一句结论、简单的数值汇总。",
     "需要权衡矛盾证据的多步推理；长链条的因果分析。"),
    ("qwen3.8-27b", "Qwen 3.8 27B",
     "中文语境下的新闻与情绪面梳理、把搜索返回的杂乱文本归纳成要点。",
     "高精度数值计算；对小数位敏感的技术指标比对。"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash",
     "带推理的中间结论。多指标交叉验证、判断形态是否成立、发现任务前提本身有问题。",
     "无 —— 综合能力最强，不确定选哪个就用它。"),
]
# 保留非示例条目，避免把用户自己加的模型冲掉
keep = [m for m in (d.get("models") or [])
        if m.get("id") and not str(m.get("id", "")).startswith("EXAMPLE")]
have = {m.get("id") for m in keep}
for mid, label, use, avoid in seeds:
    if mid in have:
        continue
    keep.append({
        "id": mid, "label": label, "base_url": base,
        "api_key_env": "", "max_tokens": 8192,
        "use_for": use, "avoid_for": avoid,
    })
d["models"] = keep
if not any(m["id"] == d.get("default") for m in keep):
    d["default"] = "glm-5.3-fast"
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已灌入三个实测可用模型（端点 {base or '(继承 LLM_BASE_URL)'}），默认 {d['default']}")
print("密钥复用 LLM_API_KEY，无需额外配置。")
PY
    chmod 644 "$MODELS_JSON"
    reload
    ;;

  --add)
    ID="${1:-}"; BASE="${2:-}"; KEYENV="${3:-}"; MT="${4:-8192}"
    [[ -n "$ID" ]] || { echo "用法: --add <id> <base_url> [api_key_env] [max_tokens]" >&2; exit 1; }
    python3 - "$MODELS_JSON" "$ID" "$BASE" "$KEYENV" "$MT" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
mid, base, keyenv, mt = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
d = json.loads(p.read_text(encoding="utf-8"))
ms = d.setdefault("models", [])
for m in ms:
    if m.get("id") == mid:
        m.update({"base_url": base or m.get("base_url", ""), "max_tokens": int(mt)})
        if keyenv:
            m["api_key_env"] = keyenv
        break
else:
    ms.append({"id": mid, "label": mid, "base_url": base, "api_key_env": keyenv,
               "max_tokens": int(mt),
               "use_for": "（未填写，建议补上以便主 Agent 正确选择）", "avoid_for": ""})
if not d.get("default"):
    d["default"] = mid
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已写入模型 {mid}")
if keyenv:
    print(f"  → 记住：还要在 .env 里加一行 {keyenv}=你的key，然后重启 brain")
else:
    print("  → 未指定 api_key_env，将回落使用 SUBAGENT_API_KEY / LLM_API_KEY")
PY
    chmod 644 "$MODELS_JSON"
    reload
    ;;

  --remove)
    ID="${1:-}"
    [[ -n "$ID" ]] || { echo "用法: --remove <id>" >&2; exit 1; }
    python3 - "$MODELS_JSON" "$ID" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); mid = sys.argv[2]
d = json.loads(p.read_text(encoding="utf-8"))
before = len(d.get("models") or [])
d["models"] = [m for m in (d.get("models") or []) if m.get("id") != mid]
if d.get("default") == mid:
    d["default"] = d["models"][0]["id"] if d["models"] else ""
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已移除 {mid}（{before} → {len(d['models'])} 个）")
PY
    chmod 644 "$MODELS_JSON"
    reload
    ;;

  --default)
    ID="${1:-}"
    [[ -n "$ID" ]] || { echo "用法: --default <id>" >&2; exit 1; }
    python3 - "$MODELS_JSON" "$ID" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); mid = sys.argv[2]
d = json.loads(p.read_text(encoding="utf-8"))
if not any(m.get("id") == mid for m in (d.get("models") or [])):
    print(f"警告：{mid} 不在白名单里，仍然设为默认值（它不会生效）")
d["default"] = mid
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"默认模型已设为 {mid}")
PY
    chmod 644 "$MODELS_JSON"
    reload
    ;;

  --off)
    python3 - "$MODELS_JSON" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text(encoding="utf-8"))
d["models"] = []; d["default"] = ""
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("白名单已清空")
PY
    chmod 644 "$MODELS_JSON"
    reload
    ;;

  *)
    sed -n '2,18p' "$0"
    exit 1
    ;;
esac
