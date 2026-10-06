#!/usr/bin/env bash
# 重建大脑，并且**当场证明烤进镜像的和磁盘上那份是同一份**。
#
# 用法：
#   bash rebuild.sh                # 重建 + 验收
#   bash rebuild.sh --check-only    # 不重建，只验收现在这个容器
#   bash rebuild.sh --force         # 有一轮心跳正在跑也照重建
#   bash rebuild.sh --selftest      # 只自检「在飞的心跳」这个检测器（不碰 docker）
#
# # 它为什么存在（待办 10，2026-09-27）
#
# 这个坑在文档里被粗体警告过两处，而当天还是被踩了四次 ——
# 项目自己两次，部署那一轮我又两次。**每一次都是侥幸**（push 比构建快），
# 每一次都是靠手工 `docker exec … md5sum` 才发现是侥幸。
#
# 结论：规则靠人记就会漏，连写规则的人自己都会漏。所以把它变成工具 ——
#
#   ① 构建**前后各留一份磁盘 md5**。构建读的是「磁盘上当时」的源码，
#      所以"有没有踩到这个坑"的判据是：构建**前**那份快照 与 容器里那份 是否逐个相同。
#      用前快照而不是后快照，是因为 push 可能正好落在构建中间 ——
#      那时候磁盘已经变了、而镜像里是旧的，用后快照比会**恰好比出"一致"**。
#   ② 一轮心跳正在跑的时候重建会把它踩断（数出来过 14 次，全在频繁重建的那晚）。
#      踩断了场上是没有收尾事件的，事后从日志上看不出来。所以先看一眼，认得出来就拦。
#
# 这个脚本**不替你做 push**。它只保证一件事：你按下重建之后，
# "镜像里到底是哪一份"这件事有证据，而且证据是在你走开之前拿到的。
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ai-trader/brain:latest
CONTAINER=trader-brain
STAMP=/tmp/rebuild-before.md5

CHECK_ONLY=0
FORCE=0
SELFTEST=0
for a in "$@"; do
  case "$a" in
    --check-only) CHECK_ONLY=1 ;;
    --force)      FORCE=1 ;;
    --selftest)   SELFTEST=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

c()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
er() { printf '\033[1;31m  !! \033[0m %s\n' "$*" >&2; }

# 烤进镜像的东西 = brain/Dockerfile 里的 COPY（trader/ 和 static/）。
# 改这两个目录里的任何文件都必须重建；config/ 里的人类文件是热加载的，不算。
#
# 口径：两个 find 都从**各自的根**出发 —— 磁盘从 `brain/` 进（镜像的 `/app` 就是它），
# 容器从 `/app` 进。所以两边的相对路径都是 `trader/…` / `static/…`，可以逐个对。
baked_files() {   # 必须在 brain/ 目录下跑；输出**相对 brain/** 的路径
  find trader static -type f \
    \( -name '*.py' -o -name '*.html' \) \
    -not -path '*/__pycache__/*' -not -name '*.bak' | sort
}

snapshot() {   # 磁盘这份的 md5（相对路径，方便跟容器里那份逐个对）
  ( cd brain && baked_files | xargs md5sum )
}

in_container() {  # 容器里那份的 md5
  docker exec "$CONTAINER" sh -c \
    'cd /app && find trader static -type f \( -name "*.py" -o -name "*.html" \) \
       -not -path "*/__pycache__/*" | sort | xargs md5sum'
}

# ---------------------------------------------------------------- 0. 在飞的心跳
# 返回 **0（真）= 有一轮心跳正在跑**；1 = 没有。名字和返回值必须同向 ——
# 第一版写反了（python 里 `exit(1 if open_beats else 0)`，而这里用 `if beat_in_flight`），
# 于是"一轮都没跑"的时候它反而拦人。**极性写反的检测器比没有检测器更坏**：
# 它拦的是正常操作，而你会开始习惯性 --force。
beat_in_flight() {
  local log="${1:-data/logs/events.jsonl}"
  python3 - "$log" <<'PY' 2>/dev/null
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
if not p.is_file():
    sys.exit(1)                      # 看不到账本 → 当作没在跑（全新环境）
tail = []
with p.open() as f:
    for line in f:
        try: tail.append(json.loads(line))
        except Exception: pass
tail = tail[-60:]
open_beats = 0
for e in tail:
    if e.get("kind") == "beat_start": open_beats += 1
    elif e.get("kind") == "beat_end": open_beats = max(0, open_beats - 1)
sys.exit(0 if open_beats else 1)      # ★ 0 = 有在飞的
PY
}

if [[ $SELFTEST -eq 1 ]]; then
  c "自检：在飞检测器的两个方向"
  printf '{"kind":"beat_start"}\n' > /tmp/rs_open.jsonl
  printf '{"kind":"beat_start"}\n{"kind":"beat_end"}\n' > /tmp/rs_closed.jsonl
  if beat_in_flight /tmp/rs_open.jsonl; then ok "未收尾 → 判为「在跑」（真阳性）"
  else er "漏报：未收尾的 beat_start 没被判为在跑 → 它会静默踩断一轮"; exit 1; fi
  if beat_in_flight /tmp/rs_closed.jsonl; then
    er "误报：已收尾的也判为在跑 → 它会拦正常操作，人会开始习惯性 --force"; exit 1
  else ok "已收尾 → 判为「没在跑」（真阴性）"; fi
  if beat_in_flight /tmp/rs_nonexistent.jsonl; then
    er "漏报：账本不存在时判为在跑"; exit 1
  else ok "账本不存在 → 不拦（全新环境）"; fi
  rm -f /tmp/rs_open.jsonl /tmp/rs_closed.jsonl
  ok "两个方向都对"
  exit 0
fi

if [[ $CHECK_ONLY -eq 0 ]]; then
  if beat_in_flight; then
    if [[ $FORCE -eq 0 ]]; then
      er "日志看着有一轮心跳正在跑（最近有 beat_start 没等到 beat_end）。"
      er "重建会把它踩断——踩断之后场上是没有收尾事件的，事后从日志上看不出来。"
      er "要么等它跑完（面板上看心跳），要么 bash rebuild.sh --force。"
      exit 1
    fi
    ok "有一轮心跳在跑，--force 放行"
  else
    ok "没有在飞的心跳（最近 60 条事件里没有未收尾的 beat_start）"
  fi

  c "重建前：留一份磁盘 md5 快照（$STAMP）"
  snapshot > "$STAMP"
  ok "$(wc -l < "$STAMP") 个文件"

  c "重建 brain"
  docker compose up -d --build brain
else
  c "只验收，不重建"
fi

# ---------------------------------------------------------------- 验收
c "验收：容器里烤的 == 磁盘上这份？（判据是**构建前**那份快照）"
if [[ $CHECK_ONLY -eq 1 ]]; then
  # 没重建就没法说"当时"，只能拿现在的磁盘比。
  snapshot > "$STAMP"
fi
in_container > /tmp/rebuild-in.md5

bad=0
while read -r sum path; do
  want=$(awk -v p="$path" '$2 == p {print $1}' "$STAMP")
  if [[ -z "$want" ]]; then
    er "$path 在快照里找不到（路径口径不一致，或它是构建之后新冒出来的文件）"
    bad=1
  elif [[ "$want" != "$sum" ]]; then
    er "$path 对不上"
    er "    镜像里 $sum"
    er "    构建前 $want"
    bad=1
  fi
done < /tmp/rebuild-in.md5

if [[ $bad -eq 1 ]]; then
  echo
  er "镜像里烤的**不是**你改的那一份 —— 多半是 push 和 rebuild 并发发了。"
  er "现在磁盘上那份是对的，所以本地测试全绿也看不出来。修法："
  er "    确认 push 已落地（md5sum），再跑一次 bash rebuild.sh"
  exit 1
fi
ok "逐个相同（$(wc -l < /tmp/rebuild-in.md5) 个文件）"

# 构建期间磁盘又被改了的话，上面那条比对**不会**发现（比的是构建前）。单独说一声。
if [[ $CHECK_ONLY -eq 0 ]]; then
  snapshot > /tmp/rebuild-after.md5
  if ! diff -q "$STAMP" /tmp/rebuild-after.md5 >/dev/null; then
    er "构建期间磁盘上的文件又变了（大概是 push 正好落在构建中间）。"
    er "这一次烤进去的是构建前那份，**不是**现在的 —— 再跑一次 bash rebuild.sh。"
    diff "$STAMP" /tmp/rebuild-after.md5 | sed 's/^/    /' || true
    exit 1
  fi
fi

echo
docker compose ps --format '  {{.Name}}  {{.Status}}'
echo
ok "可以走开了。"
