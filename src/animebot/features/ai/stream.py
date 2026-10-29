"""把生成中的回答「长」在一条消息上。

**为什么放弃了 `sendMessageDraft`。** 那个接口画的是**临时草稿**：30 秒自动
消失、`message_id` 恒为 0、生成结束还得再 `sendMessage` 一次落库。实测在客户端
上草稿和正式消息**同时可见**（用户截图里那两条内容相同的消息），要等草稿
自己到期才消失 —— 看起来就是「bot 回了两遍」。而且它只支持私聊。

所以改成最朴素的办法：**先发一条占位消息，然后原地 edit 它**。一条消息从头
长到尾，聊天记录里始终只有一条，私聊和群里的 ephemeral 都适用
（`bot.ui.edit_text` 负责这两条 API 的分叉）。

## 为什么要自己控制「显示进度」

实测网关的吐字节奏（2026-10-29，deepseek-v4.1-flash）：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 思考（强制 thinking，关不掉） | 1~3s | 一个正文 token 都没有 |
| 工具调用 | 0.3~4s | Bangumi / Bing 往返 |
| 正文 | **0.7s 出 130 个 delta** | 一次性涌进来 |

正文只有一个「突发」，不是稳定细流。直接把最新文本刷上去的话，用户在
0.7 秒里看不到任何中间态 —— 观感就是「没有流式」。所以这里加了一层**回放**：
每次 tick 只把接收缓冲区往前推一段（约剩余量的 35%，最少 24 字），
450 字的回答大约 2 秒长完。这不是造假，是把「已经拿到的内容」按人能读的
节奏放出来；`finish()` 仍然一次写完整文本，不做任何截断。

## 节流

一手实测：同一条消息连刷 0.2s 一版 10/10 全过、不报 429（编辑不像
`sendMessage` 那样每次都推通知）。0.25s 是「看得出在长」和「别浪费往返」的
折中。撞 429 就把间隔乘 1.6（上限 6s），重试这一版，不做重试风暴。
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

#: 两次编辑之间的间隔（按「上一次发出」起算，含网络往返）。
_MIN_INTERVAL = 0.25
_MAX_INTERVAL = 6.0
#: 单条消息的字符预算。Telegram 上限 4096，HTML 实体还会再涨一些。
_MESSAGE_CAP = 3500
#: 每次 tick 至少推进这么多字。太小会让短回答磨很久，太大就回到「一次蹦完」。
_REVEAL_MIN = 24
#: 每次 tick 推进剩余量的比例。0.35 让 450 字大约 2 秒长完。
_REVEAL_RATIO = 0.35

#: 续篇发送器：收到（可带 parse_mode=None 的）文本，发出去。由 handler 提供，
#: 群里走 ephemeral。超长回答和 HTML 降级都靠它补后续分片。
SendExtra = Callable[..., Awaitable[object]]


def compose(notice: str, body: str) -> str:
    """占位消息正文：状态提示一行 + 已生成的部分。"""
    body = body.strip()
    if notice and body:
        return f"{notice}\n\n{body}"
    return notice or body


@dataclass(slots=True)
class EditStreamer:
    """占位消息 + 节流原地编辑 + 正文回放。

    `on_text` 是同步回调（在解析 SSE 的循环里调），只记文本并置 dirty；
    真正的网络请求和回放节奏由 `pump` 协程控制 —— 在解析循环里 await 会拖慢读流。
    """

    bot: Bot
    sent: Sent
    render: Callable[[str], str]
    send_extra: SendExtra
    interval: float = _MIN_INTERVAL
    _notice: str = ""
    _body: str = ""
    _revealed: int = 0
    _shown: str = ""
    _dirty: asyncio.Event = field(default_factory=asyncio.Event)
    _stopping: bool = False
    _broken: bool = False

    # ---------------------------------------------------------- 输入
    def on_text(self, body: str) -> None:
        """收到「到目前为止的完整正文」。回放进度不倒退，除非正文变短了。"""
        self._body = body
        self._revealed = min(self._revealed, len(body))
        self._dirty.set()

    def set_notice(self, text: str) -> None:
        """工具调用 / 思考进度这类状态行。立刻刷一版，不等回放。"""
        self._notice = text
        self._dirty.set()

    # ---------------------------------------------------------- 泵
    async def pump(self) -> None:
        """有变化就按节流编辑一版。被 cancel 时静默退出。

        节流按「两次**发出**之间的间隔」算：`_flush` 本身要一个网络往返
        （VPS 直连约 150ms），把它算进间隔才能保证节奏稳定。
        """
        loop = asyncio.get_running_loop()
        try:
            while not self._stopping:
                await self._dirty.wait()
                self._dirty.clear()
                if self._stopping:
                    return
                started = loop.time()
                await self._flush()
                # 回放还没追上接收缓冲区：这一版不是终点，继续排下一版。
                # 少了这一步，正文会在第一版之后就停住，等下一次真实 delta 才动。
                if self._revealed < len(self._body) and not self._broken:
                    self._dirty.set()
                await asyncio.sleep(max(0.0, self.interval - (loop.time() - started)))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ai.stream_pump_crashed")

    async def _flush(self) -> None:
        if self._broken:
            return
        self._advance()
        text = self.render(self._visible[:_MESSAGE_CAP])
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

    def _advance(self) -> None:
        """把回放进度往前推一段。收尾阶段（`_stopping`）直接到头。"""
        remaining = len(self._body) - self._revealed
        if remaining <= 0:
            return
        if self._stopping:
            self._revealed = len(self._body)
            return
        step = max(_REVEAL_MIN, int(remaining * _REVEAL_RATIO + 0.999))
        self._revealed = min(len(self._body), self._revealed + step)

    @property
    def _visible(self) -> str:
        return compose(self._notice, self._body[: self._revealed])

    @property
    def latest(self) -> str:
        """当前已经在屏幕上显示的内容。用户按停止时用来保留半截答案。"""
        return self._visible

    # ---------------------------------------------------------- 收尾
    def stop(self) -> None:
        self._stopping = True
        self._dirty.set()

    async def finish(self, markdown: str, **kw: object) -> None:
        """把最终回答写进占位消息；超长时其余分片另起消息。

        这里不经过回放：`_stopping` 置位后 `_advance` 直接到头，完整的最终
        文本一次写完 —— 收尾必须准，不能因为「还没播完」而截断。
        """
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
