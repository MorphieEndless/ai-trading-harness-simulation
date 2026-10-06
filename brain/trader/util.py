"""小工具函数。"""
from __future__ import annotations

from typing import Any


def flatten_content(value: Any) -> str:
    """把 message.content 归一成字符串。

    某些 OpenAI 兼容端点（尤其代理到 Gemini / Claude 的）会把 content 返回成
    **数组**而不是字符串，形如：
        [{"type": "text", "text": "..."}]
        [{"text": "..."}, {"text": "..."}]
        ["a", "b"]
    直接 .strip() 会抛 AttributeError，所以统一在这里收口。

    实测：本项目的 glm-5.3-fast 就会返回数组形式，不处理会整个子代理调用失败。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "value"):
                    v = item.get(key)
                    if isinstance(v, str):
                        parts.append(v)
                        break
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            v = value.get(key)
            if isinstance(v, str):
                return v
    return str(value)
