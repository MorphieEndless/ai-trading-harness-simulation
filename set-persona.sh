#!/usr/bin/env bash
# 管理 Agent 的人格（config/persona.md）。
#
# 用法：
#   bash set-persona.sh --show              查看当前人格
#   bash set-persona.sh --clear             清空（回到默认无性格状态）
#   bash set-persona.sh --example [A|B|C]   从示例里挑一个灌进去
#   bash set-persona.sh --file my.md        用本地文件覆盖
#   bash set-persona.sh                     直接用 stdin（粘完按 Ctrl-D）
#
# 改完立即生效，不用重启 —— Agent 每次心跳都会重新读这个文件。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PERSONA="config/persona.md"
EXAMPLE="config/persona.example.md"

[[ -d config ]] || { echo "缺少 config 目录，请先跑 setup.sh" >&2; exit 1; }

status() {
  local n
  n=$(wc -c < "$PERSONA" 2>/dev/null || echo 0)
  if [[ "$n" -le 1 ]]; then
    echo "人格：未设置（Agent 使用默认性格，不加角色扮演层）"
  else
    echo "人格：已加载 ${n} 字符（下次心跳起生效）"
  fi
}

case "${1:-}" in
  --show)
    echo "----- $PERSONA -----"
    if [[ -s "$PERSONA" ]]; then cat "$PERSONA"; else echo "(空)"; fi
    echo "----- 状态 -----"
    status
    ;;

  --clear)
    : > "$PERSONA"
    chmod 644 "$PERSONA"
    status
    ;;

  --example)
    WHICH="${2:-A}"
    [[ -f "$EXAMPLE" ]] || { echo "找不到 $EXAMPLE" >&2; exit 1; }
    python3 - "$EXAMPLE" "$PERSONA" "$WHICH" <<'PY'
import pathlib, sys, re
src, dst, which = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3].upper()
text = src.read_text(encoding="utf-8")
# 抓 "## 示例 X：..." 到下一个 "## " 之间的内容
pat = re.compile(rf"^##\s*示例\s*{re.escape(which)}\s*[：:](.+?)(?=^##\s|\Z)",
                 re.MULTILINE | re.DOTALL)
m = pat.search(text)
if not m:
    print(f"示例 {which} 没找到", file=sys.stderr)
    raise SystemExit(1)
title, body = m.group(1).split("\n", 1)
body = body.strip()
dst.write_text(body + "\n", encoding="utf-8")
print(f"已灌入示例 {which}：{title.strip()}（{len(body)} 字符）")
PY
    chmod 644 "$PERSONA"
    status
    echo "提示：这只是起点，直接编辑 $PERSONA 改成你想要的。"
    ;;

  --file)
    SRC="${2:-}"
    [[ -f "$SRC" ]] || { echo "用法: --file <路径>" >&2; exit 1; }
    cp "$SRC" "$PERSONA"
    chmod 644 "$PERSONA"
    status
    ;;

  "")
    if [[ -t 0 ]]; then
      echo "从标准输入读取人格内容，粘贴完按 Ctrl-D 结束："
    fi
    cat > "$PERSONA"
    chmod 644 "$PERSONA"
    status
    ;;

  *)
    sed -n '2,14p' "$0"
    exit 1
    ;;
esac
