"""Anthropic Messages 协议的流式客户端（`/v1/messages` + SSE）。

为什么手写 SSE 解析而不是用官方 SDK：这里只需要「把 text_delta 抽出来」，
而整条链路（工具循环、取消、超时、埋点）本来就得自己写。多一个 SDK 就多一份
依赖、一份版本兼容、一份它自己的超时语义，换不来什么。

网关实测（2026-10-28）：
  - 模型**强制 thinking**，`thinking.type=disabled` 会 400；
  - thinking 块不能原样回传给下一轮（会报错），发回去之前必须剥掉；
  - 客户端工具调用（tool_use / tool_result）正常，服务端工具（web_search_20250305）
    会被静默丢弃 —— 所以联网搜索必须我们自己做。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ...config import Settings
from ...core.errors import ExternalServiceError
from ...observability.logging import get_logger
from ...observability.metrics import METRICS

log = get_logger("animebot.ai.client")

_ANTHROPIC_VERSION = "2023-06-01"
# 网关会在 thinking 上花掉大量 token，200 太小写不完一句话。
_MIN_TOKENS = 256


class Stopped(Exception):
    """用户按了 Telegram 上的「停止生成」，或者是超时前的主动中止。"""


@dataclass(slots=True)
class Round:
    """一轮回答的结果。`content` 已经剥掉 thinking，可以直接回流给下一轮。"""

    content: list[dict[str, Any]]
    text: str
    stop_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)


async def iter_sse(lines: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """把 SSE 字节行流解析成事件 dict。

    `data:` 可能跨多行，事件之间用空行分隔。`event:` 行整个忽略 —— 每帧的
    `data` 里本来就带 `type`，用它分类比跟踪 event 名更可靠。
    """
    buf: list[str] = []
    async for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if not buf:
                continue
            payload = "\n".join(buf)
            buf.clear()
            if payload.strip() == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                log.warning("ai.sse_bad_json", head=payload[:120])
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
        # 注释行（": ping"）和 event: 行都不用管
    if buf:   # 流没有以空行结尾
        with contextlib.suppress(json.JSONDecodeError):
            yield json.loads("\n".join(buf))


def sse_lines(chunks: list[str]) -> AsyncIterator[bytes]:
    """测试和离线回放用：把整段文本当作行流喂进去。"""

    async def gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            for line in chunk.splitlines(keepends=True):
                yield line.encode()

    return gen()


class ClaudeClient:
    """一个实例对应一个网关。工具循环也在这里 —— 它和协议细节绑死。"""

    def __init__(self, settings: Settings, *, session: aiohttp.ClientSession | None = None) -> None:
        self._cfg = settings
        self._session = session
        self._owns_session = session is None

    # ---------------------------------------------------------- 生命周期
    async def open(self) -> None:
        if self._session is None or self._session.closed:
            # aiohttp 不读 http_proxy 环境变量，必须显式传。本机直连被墙时
            # （ANIMEBOT_PROXY 非空）和 Telegram 用同一个代理；VPS 上为空直连。
            proxy = self._cfg.proxy or None
            self._session = aiohttp.ClientSession(proxy=proxy) if proxy else (
                aiohttp.ClientSession()
            )
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def configured(self) -> bool:
        return bool(self._cfg.ai_api_key.get_secret_value())

    # ---------------------------------------------------------- 一轮流式
    async def stream_round(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_text: Any = None,
        on_thinking: Any = None,
        cancel: asyncio.Event | None = None,
        model: str | None = None,
    ) -> Round:
        """发一轮请求，边收边回调。返回时 `content` 已经是可回流的形状。"""
        body: dict[str, Any] = {
            "model": model or self._cfg.ai_model,
            "max_tokens": max(self._cfg.ai_max_tokens, _MIN_TOKENS),
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if system:
            body["system"] = [{"type": "text", "text": system}]

        session = await self._open_session()
        url = f"{self._cfg.ai_base_url.rstrip('/')}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": self._cfg.ai_api_key.get_secret_value(),
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        timeout = aiohttp.ClientTimeout(total=self._cfg.ai_timeout)

        blocks: dict[int, dict[str, Any]] = {}
        partial_json: dict[int, str] = {}
        text_parts: list[str] = []
        stop_reason: str | None = None
        usage: dict[str, Any] = {}

        started = asyncio.get_running_loop().time()
        try:
            async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    METRICS.incr("ai.http_error", model=self._cfg.ai_model, status=resp.status)
                    raise ExternalServiceError("AI", f"HTTP {resp.status}: {detail}")

                async for ev in iter_sse(resp.content):
                    if cancel is not None and cancel.is_set():
                        raise Stopped

                    kind = ev.get("type")
                    if kind == "content_block_start":
                        idx = int(ev.get("index", 0))
                        block = dict(ev.get("content_block") or {})
                        block.setdefault("type", "text")
                        if block["type"] == "text":
                            block.setdefault("text", "")
                        blocks[idx] = block
                        if block["type"] == "tool_use":
                            block.setdefault("input", {})
                            METRICS.incr("ai.tool_call", tool=block.get("name", "?"))

                    elif kind == "content_block_delta":
                        idx = int(ev.get("index", 0))
                        delta = ev.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            piece = delta.get("text", "")
                            text_parts.append(piece)
                            cur = blocks.setdefault(idx, {"type": "text", "text": ""})
                            cur["text"] = cur.get("text", "") + piece
                            if on_text is not None:
                                on_text(piece)
                        elif dtype == "thinking_delta" and on_thinking is not None:
                            on_thinking(delta.get("thinking", ""))
                        elif dtype == "input_json_delta":
                            partial_json[idx] = partial_json.get(idx, "") + delta.get(
                                "partial_json", ""
                            )

                    elif kind == "message_delta":
                        stop_reason = (ev.get("delta") or {}).get("stop_reason")
                        usage.update(ev.get("usage") or {})

                    elif kind == "error":
                        detail = json.dumps(ev.get("error"), ensure_ascii=False)[:300]
                        raise ExternalServiceError("AI", detail)

        except TimeoutError as exc:
            METRICS.incr("ai.timeout", model=self._cfg.ai_model)
            raise ExternalServiceError("AI", "生成超时") from exc
        except aiohttp.ClientError as exc:
            METRICS.incr("ai.client_error", model=self._cfg.ai_model)
            raise ExternalServiceError("AI", repr(exc)) from exc
        finally:
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            METRICS.observe("ai.latency", round(elapsed, 1), model=self._cfg.ai_model)

        content: list[dict[str, Any]] = []
        for idx in sorted(blocks):
            block = blocks[idx]
            if block["type"] == "tool_use":
                raw = partial_json.get(idx)
                if raw:
                    try:
                        block["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("ai.tool_json_bad", tool=block.get("name"), raw=raw[:200])
                        block["input"] = {}
                block.pop("input_partial", None)
            # thinking 不回流：网关回传 thinking 块会 400（实测）
            if block["type"] in ("text", "tool_use"):
                content.append(block)

        return Round(
            content=content,
            text="".join(text_parts),
            stop_reason=stop_reason,
            usage=usage,
        )

    async def _open_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            await self.open()
        assert self._session is not None
        return self._session
