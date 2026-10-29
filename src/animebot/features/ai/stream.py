"""把模型的回答送到 Telegram。

Telegram 的流式只有一条路：`sendMessageDraft`（Bot API 9.5+）。它画的是
**临时草稿** —— 用户聊天里那条消息 30 秒后自动消失，等生成结束必须再用
`sendMessage` 发一条完整的落库。所以流程是：

    draft("") → draft("进击") → draft("进击的巨人评分 8.2") → sendMessage(全文)

代价是用户会看到「闪烁变一次的草稿 + 最终消息」，但这是平台唯一给的能力：
Bot API 没有 editMessageText 级别的增量渲染（edit 一次就是一个完整的 Message，
每次都刷一版会让聊天记录里跳动、也更快撞限流）。

`sendMessageDraft` 只在**私聊**可用（文档明确写了 private chat）。群里本来
就走 ephemeral 回复，那边不做流式：草稿没有 ephemeral 版本，会把别人的
搜索过程公开。

限流：Telegram 对同一 chat 的 sendMessage 有 ~1 条/秒的软限。草稿同理，
所以这里按 `_MIN_INTERVAL` 节流，宁可少刷几次也不撞 429。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from ...observability.logging import get_logger

log = get_logger("animebot.ai.stream")

# 两次草稿之间的最小间隔。1.2s 实测稳定；再快就容易撞 Flood control。
_MIN_INTERVAL = 1.2
# 草稿最大长度，和 Telegram 的 4096 留点余量（解析成 HTML 后实体会变长）。
_DRAFT_CAP = 3500


@dataclass(slots=True)
class DraftStreamer:
    """把不断增长的文本推成 Telegram 草稿。

    `on_text` 是同步回调（客户端在解析 SSE 的循环里调它），内部只记录最新文本
    并置一个 dirty 标记，真正的网络请求由 `pump` 协程按节流发出去 —— 不能在
    解析循环里 await，那会拖慢读流。
    """

    bot: Bot
    chat_id: int
    draft_id: int
    render: Callable[[str], str]
    interval: float = _MIN_INTERVAL
    _latest: str = ""
    _sent: str = ""
    _dirty: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: bool = False
    _failures: int = 0

    def on_text(self, text: str) -> None:
        self._latest = text
        self._dirty.set()

    def push_now(self, text: str) -> None:
        """工具调用之类的关键节点，无视节流立刻推一版。"""
        self._latest = text
        self._sent = ""
        self._dirty.set()

    async def pump(self) -> None:
        """后台协程：有变化就按节流发一版。被 cancel 时静默退出。"""
        try:
            while not self._stop:
                await self._dirty.wait()
                self._dirty.clear()
                if self._stop:
                    return
                await self._flush()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ai.draft_pump_crashed")

    async def _flush(self) -> None:
        text = self.render(self._latest[:_DRAFT_CAP])
        if text == self._sent:
            return
        try:
            await self.bot.send_message_draft(
                chat_id=self.chat_id, draft_id=self.draft_id, text=text
            )
            self._sent = text
            self._failures = 0
        except TelegramRetryAfter as exc:
            log.info("ai.draft_retry_after", retry_after=exc.retry_after)
            await asyncio.sleep(min(exc.retry_after, 5))
        except TelegramAPIError as exc:
            # 老客户端 / 频道 / 群聊：草稿不可用。不是错误，静默降级成「最后一次性发」。
            self._failures += 1
            if self._failures == 1:
                log.info("ai.draft_unavailable", chat_id=self.chat_id,
                         error=str(exc)[:120])
            self._stop = True

    def stop(self) -> None:
        self._stop = True
        self._dirty.set()

    @property
    def supported(self) -> bool:
        return self._failures == 0


def split_text(text: str, limit: int = 3800) -> list[str]:
    """按段落边界切分，尽量不把一句话劈开。空输入返回一个占位串。"""
    text = text.strip()
    if not text:
        return ["（模型没有返回内容）"]
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


class Throttle:
    """简单的令牌节流。给「工具调用提示」这类非关键消息用。"""

    def __init__(self, interval: float = _MIN_INTERVAL) -> None:
        self._interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False
