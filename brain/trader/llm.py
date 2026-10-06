"""OpenAI 兼容的模型客户端。只认 base_url + api_key + model 三件事，
不假设是哪家供应商，也不做任何供应商特判。"""
from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI

from .config import Config
from .util import flatten_content

log = logging.getLogger("trader.llm")


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if not self.cfg.llm_configured:
            raise LLMError("模型未配置：请在 .env 里填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.cfg.llm_base_url,
                api_key=self.cfg.llm_api_key,
                timeout=self.cfg.llm_timeout,
                max_retries=2,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": self.cfg.llm_temperature,
            "max_tokens": self.cfg.llm_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc)
            # 部分自建端点不接受 tools / tool_choice，明确报错时降级重试一次
            if tools and ("tool" in msg.lower() and ("support" in msg.lower() or "unsupported" in msg.lower())):
                log.warning("端点似乎不支持 tool calling，降级为纯文本重试：%s", msg[:200])
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                try:
                    resp = await self.client.chat.completions.create(**kwargs)
                except Exception as exc2:
                    raise LLMError(f"模型调用失败：{type(exc2).__name__}: {exc2}") from exc2
            else:
                raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        if not resp.choices:
            raise LLMError("模型返回了空的 choices")
        msg = resp.choices[0].message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments or "{}",
            })
        usage = getattr(resp, "usage", None)

        # 思考型模型（Gemini Thinking / DeepSeek / o 系等）会把思维链放在
        # 非标准字段里。openai SDK 会把它收进 model_extra，这里捞出来，
        # 一方面能在面板上展示，另一方面便于发现「思考吃光 token 预算」。
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = ""
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = extra.get(key)
            if isinstance(val, str) and val.strip():
                reasoning = val
                break

        return {
            "content": flatten_content(msg.content),
            "reasoning": reasoning,
            "tool_calls": tool_calls,
            "finish_reason": resp.choices[0].finish_reason,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            } if usage else None,
        }
