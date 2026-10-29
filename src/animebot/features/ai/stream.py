"""把生成中的回答「长」在一条消息上。

**为什么放弃了 `sendMessageDraft`。** 那个接口画的是**临时草稿**：30 秒自动
消失、`message_id` 恒为 0、生成结束还得再 `sendMessage` 一次落库。实测在客户端
上草稿和正式消息**同时可见**（用户截图里那两条内容相同的消息），要等草稿
自己到期才消失 —— 看起来就是「bot 回了两遍」。而且它只支持私聊。

所以改成最朴素的办法：**先发一条占位消息，然后原地 edit 它**。一条消息从头
长到尾，聊天记录里始终只有一条，私聊和群里的 ephemeral 都适用
（`bot.ui.edit_text` 负责这两条 API 的分叉）。代价是编辑有节流 —— Telegram
对同一聊天大约 1 次/秒，所以按 `interval` 攒一版再刷；撞 429 就把间隔乘上去，
不做重试风暴。

最终文本可能超过 4096，所以要分片：第一片编辑进占位消息，其余各发一条
（走调用方给的 `send_extra`，它知道群里该不该用 ephemeral）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ...bot.ui import Sent, edit_text
from ...observability.logging import get_logger

log = get_logger("animebot.ai.stream")

#: 两次编辑之间的间隔。1 秒是 Telegram 的软限，留点余量。
_MIN_INTERVAL = 1.4
_MAX_INTERVAL = 6.0
#: 单条消息的字符预算。Telegram 上限 4096，HTML 实体还会再涨一些。
_MESSAGE_CAP = 3500

#: 续篇发送器：收到（可带 parse_mode=None 的）文本，发出去。由 handler 提供，
#: 群里走 ephemeral。超长回答和 HTML 降级都靠它补后续分片。
SendExtra = Callable[..., Awaitable[object]]


@dataclass(slots=True)
class EditStreamer:
    """占位消息 + 节流原地编辑。

    `on_text` 是同步回调（在解析 SSE 的循环里调），只记最新文本并置 dirty；
    真正的网络请求由 `pump` 协程发 —— 在解析循环里 await 会拖慢读流。
    """

    bot: Bot
    sent: Sent
    render: Callable[[str], str]
    send_extra: SendExtra
    interval: float = _MIN_INTERVAL
    _latest: str = ""
    _shown: str = ""
    _dirty: asyncio.Event = field(default_factory=asyncio.Event)
    _stopping: bool = False
    _broken: bool = False

    # ---------------------------------------------------------- 输入
    def on_text(self, text: str) -> None:
        self._latest = text
        self._dirty.set()

    def push_now(self, text: str) -> None:
        """工具调用之类的关键节点：改内容并立刻刷一版。"""
        self._latest = text
        self._dirty.set()

    # ---------------------------------------------------------- 泵
    async def pump(self) -> None:
        """有变化就按节流编辑一版。被 cancel 时静默退出。"""
        try:
            while not self._stopping:
                await self._dirty.wait()
                self._dirty.clear()
                if self._stopping:
                    return
                await self._flush()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ai.stream_pump_crashed")

    async def _flush(self) -> None:
        if self._broken:
            return
        text = self.render(self._latest[:_MESSAGE_CAP])
        if not text or text == self._shown:
            return
        try:
            await edit_text(self.bot, self.sent, text)
            self._shown = text
        except TelegramRetryAfter as exc:
            # 撞限流：放慢节奏，这一版稍后重试。可恢复，不算故障。
            self.interval = min(max(self.interval * 1.6, _MIN_INTERVAL), _MAX_INTERVAL)
            log.info("ai.stream_throttled", retry_after=exc.retry_after,
                     interval=round(self.interval, 1))
            await asyncio.sleep(min(exc.retry_after, 5))
            self._dirty.set()
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            self._broken = True
            log.info("ai.stream_edit_failed", error=str(exc)[:120])

    # ---------------------------------------------------------- 收尾
    def stop(self) -> None:
        self._stopping = True
        self._dirty.set()

    @property
    def latest(self) -> str:
        """到目前为止显示过的最新文本。用户按停止时用来保留半截答案。"""
        return self._latest

    async def finish(self, markdown: str, **kw: object) -> None:
        """把最终回答写进占位消息；超长时其余分片另起消息。"""
        self._stopping = True
        chunks = split_text(markdown)
        first, rest = chunks[0], chunks[1:]
        try:
            await edit_text(self.bot, self.sent, self.render(first), **kw)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        for chunk in rest:
            await self.send_extra(self.render(chunk))

    async def finish_plain(self, markdown: str, **kw: object) -> None:
        """HTML 解析失败时的退路：同样的分片，但不带 parse_mode。

        必须**编辑**占位消息而不是另发 —— 另发就又变成两条重复内容了。
        """
        self._stopping = True
        chunks = split_text(markdown)
        first, rest = chunks[0], chunks[1:]
        try:
            await edit_text(self.bot, self.sent, first, parse_mode=None, **kw)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        for chunk in rest:
            await self.send_extra(chunk, parse_mode=None)


def split_text(text: str, limit: int = _MESSAGE_CAP) -> list[str]:
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
