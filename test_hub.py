#!/usr/bin/env python3
"""MCP 返回值装配的回归测试。纯逻辑，不联网、不花钱。

守的是一个**会静默吞数据**的坑：

  FastMCP 把工具返回的 list 拆成「一个元素一个 text block」。
  hub 原来用 `\\n` 把它们拼起来，于是

      [{...}, {...}]  →  "{\\n ...\\n}\\n{\\n ...\\n}"

  这不是合法 JSON。而下游 `_safe_call` 解析失败时会**安静降级成 `[]`** ——
  于是"真的没有数据"和"解析失败"在调用方看起来一模一样，
  整类工具的返回值被吞掉，日志里连一行报错都没有。

  这个坑是在接价格监控时才暴露的：`market__get_prices` 之前从没被调过
  （Agent 一直用一次性全覆盖的 get_market_overview），
  所以它坏了很久也没人发现。

跑法：bash runtests.sh hub
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def _find_hub() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "brain" / "trader" / "mcp_hub.py",
                 here / "trader" / "mcp_hub.py",
                 pathlib.Path("/t/trader/mcp_hub.py")):
        if cand.is_file():
            return cand
    sys.exit("找不到 mcp_hub.py")


# mcp_hub 会 import mcp 包（容器里有，本地可能没有）。用一个 stub 顶掉，
# 因为 _join_blocks 是纯函数，跟 MCP 协议无关。
import types  # noqa: E402

if "mcp" not in sys.modules:
    stub = types.ModuleType("mcp")
    stub.ClientSession = object
    sw = types.ModuleType("mcp.client.streamable_http")
    sw.streamablehttp_client = object
    cm = types.ModuleType("mcp.client")
    sys.modules.setdefault("mcp", stub)
    sys.modules.setdefault("mcp.client", cm)
    sys.modules.setdefault("mcp.client.streamable_http", sw)

spec = importlib.util.spec_from_file_location("hub_under_test", _find_hub())
hub_mod = importlib.util.module_from_spec(spec)
sys.modules["hub_under_test"] = hub_mod
spec.loader.exec_module(hub_mod)

join = hub_mod._join_blocks


def main() -> int:
    print("=" * 68)
    print("MCP 返回值装配 · 纯逻辑测试")
    print("=" * 68)

    print("\n[1] 列表被拆成多块时必须重装配成 JSON 数组")
    blocks = [
        '{\n  "symbol": "BTCUSDT",\n  "price": 84000.07\n}',
        '{\n  "symbol": "ETHUSDT",\n  "price": 2683.77\n}',
    ]
    out = join(blocks)
    try:
        v = json.loads(out)
        ok = isinstance(v, list) and len(v) == 2 and v[0]["symbol"] == "BTCUSDT"
    except Exception as exc:
        ok = False
        out += f"  ({type(exc).__name__})"
    check("两块对象 → 一个合法 JSON 数组", ok, out[:120])

    print("\n[2] 单块原样透传（不要多此一举地包一层数组）")
    one = '{\n  "sleeping": false,\n  "debt_hours": 0.0\n}'
    check("单块 dict 原样返回", join([one]) == one)
    arr = '[{"a": 1}]'
    check("单块数组原样返回", join([arr]) == arr)

    print("\n[3] 非 JSON 的多块要原样拼接（别把它吃掉）")
    text = ["第一行", "第二行"]
    check("普通文本多块 → 换行拼接", join(text) == "第一行\n第二行", repr(join(text)))
    mixed = ['{"a": 1}', "这不是 JSON"]
    check("混合内容 → 退回文本拼接（宁可不装配也不丢数据）",
          join(mixed) == '{"a": 1}\n这不是 JSON', repr(join(mixed)))

    print("\n[4] 边界")
    check("空列表 → (空结果)", join([]) == "(空结果)", repr(join([])))
    check("三个块也照样装配",
          len(json.loads(join(['{"i":1}', '{"i":2}', '{"i":3}']))) == 3)

    print("\n[5] 自我校验：老写法确实是坏的（防以后有人改回去）")
    old = "\n".join(blocks)
    broke = False
    try:
        json.loads(old)
    except json.JSONDecodeError:
        broke = True
    check("直接 `\\n`.join 的产物解析不了 —— 这不是理论风险", broke)

    print("\n" + "=" * 68)
    if FAILS:
        print(f"  失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"    · {f}")
        print("=" * 68)
        return 1
    print("  全部通过")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
