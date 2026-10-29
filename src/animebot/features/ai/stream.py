"""把生成中的回答「长」到屏幕上。两种落地方式，按场景选。

## 两种方式各自解决什么问题

**`sendMessageDraft`（草稿流）—— 私聊默认。**
它的更新是**客户端动画过渡**的（同一个 `draft_id` 连发，客户端把文本平滑推移），
所以哪怕 1.7 版/秒，看起来也是顺滑地在打字。代价：

- 草稿是临时的（30 秒到期），`message_id` 恒为 0，结束必须再发一条正式消息；
- 只支持**私聊**，群里没法用；
- 没有 `reply_markup`，做不了自定义按钮 —— 但 `can_stop=True` 会由 Telegram
  自带一个「停止生成」按钮，比自己做还自然。

**`editMessageText`（原地编辑）—— 群聊 / 草稿不可用时。**
先发一条占位消息再反复改。没有动画，只有硬替换，而 `editMessageText` 一次
约 340ms 往返，最多 3 版/秒 —— 看得出是一跳一跳的。它换来的是「只有一条消息」
和群里可用的 ephemeral 支持。

## 两条路共用的部分：回放

实测网关的吐字节奏（2026-10-29，deepseek-v4.1-flash）：

| 阶段 | 耗时 | 产出 |
|---|---|---|
| 思考（强制 thinking，关不掉） | 1~3s | 零个正文 token |
| 工具调用 | 0.3~4s | 零个正文 token |
| 正文 | **0.7s 出 130 个 delta** | 一整段，一次性涌进来 |

正文只有一个「突发」。直接刷最新文本的话，用户在 0.7 秒里看不到任何中间态。
所以两种方式都走同一层回放：`on_text` 收到完整正文进缓冲区，每次 tick 只把
**显示进度**往前推一段。回放的全是已经拿到的真实内容，`finish()` 一次写完整
文本，不会因为「还没播完」而截断。

## 草稿和正式消息不能并存

`sendMessageDraft` 的残留问题是这次改动的起因：草稿和正式消息同时在聊天里
显示，看起来像 bot 回了两遍。文档说「bot 发消息时草稿会消失」，实测只有
**在草稿请求都落地之后**发消息才成立 —— 所以 `settle()` 会在收尾前等一个
往返，把可能在途的草稿消化掉。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ...bot.ui import Sent, edit_text
from ...observability.logging import get_logger

log = get_logger("animebot.ai.stream")

#: 两次更新之间的间隔（按「上一次发出」起算，含网络往返）。
#: 草稿实测 0.25s 稳、0.20s 开始撞 429；编辑实测 0.2s 连刷 10 版全过。
#: 两者的真实上限都是 RTT（VPS 到 Telegram 约 340ms），所以这个值只是下限。
_DRAFT_INTERVAL = 0.3
_EDIT_INTERVAL = 0.25
_MAX_INTERVAL = 6.0
#: 单条消息的字符预算。Telegram 上限 4096，HTML 实体还会再涨一些。
_MESSAGE_CAP = 3500
#: 每次 tick 至少推进这么多字。
_REVEAL_MIN = 12
#: 每次 tick 推进剩余量的比例。越小越平滑、越长。
_DRAFT_REVEAL = 0.22      # 草稿有动画，可以慢一点，更像在打字
_EDIT_REVEAL = 0.35       # 编辑没有动画，太慢会显得卡住
#: 收尾前等一个往返，让在途的草稿落地（否则它会和正式消息并存）。
_SETTLE = 0.5

#: 续篇发送器：收到（可带 parse_mode=None 的）文本，发出去。由 handler 提供，
#: 群里走 ephemeral。超长回答和 HTML 降级都靠它补后续分片。
SendExtra = Callable[..., Awaitable[object]]


def compose(notice: str, body: str) -> str:
    """消息正文：状态提示一行 + 已生成的部分。"""
    body = body.strip()
    if notice and body:
        return f"{notice}\n\n{body}"
    return notice or body


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


class _Pacer:
    """回放 + 节流的公共部分。子类只实现 `_push`。

    不用 dataclass：子类要加必填字段，而基类字段都有默认值，继承会把
    「非默认参数跟在默认参数后面」变成硬错误。
    """

    def __init__(
        self,
        *,
        render: Callable[[str], str],
        interval: float,
        reveal_ratio: float,
    ) -> None:
        self.render = render
        self.interval = interval
        self.reveal_ratio = reveal_ratio
        self._notice = ""
        self._body = ""
        self._revealed = 0
        self._shown = ""
        self._dirty = asyncio.Event()
        self._stopping = False
        self._broken = False

    # ---------------------------------------------------------- 输入
    def on_text(self, body: str) -> None:
        """收到「到目前为止的完整正文」。

        正文一出现就撤掉状态行：它只是「现在在干什么」的进度提示，真内容来了
        就没有意义了。留着会在收尾时突然消失，看起来像消息跳了一下。
        """
        if body and self._notice:
            self._notice = ""
        self._body = body
        self._revealed = min(self._revealed, len(body))
        self._dirty.set()

    def set_notice(self, text: str) -> None:
        """工具调用 / 思考进度这类状态行。立刻刷一版，不等回放。

        但正文已经在流的时候不抢屏 —— 那会把用户正在读的答案换成一句提示。
        """
        if self._body:
            return
        self._notice = text
        self._dirty.set()

    # ---------------------------------------------------------- 泵
    async def pump(self) -> None:
        """有变化就按节流推一版。被 cancel 时静默退出。

        节流按「两次**发出**之间的间隔」算：`_push` 本身要一个网络往返，
        把它算进间隔才能保证节奏稳定。
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
                # 回放还没追上缓冲区：这一版不是终点，继续排下一版。
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
            await self._push(text)
            self._shown = text
        except TelegramRetryAfter as exc:
            # 撞限流：放慢节奏，这一版稍后重试。可恢复，不算故障。
            self.interval = max(self.interval, _DRAFT_INTERVAL) * 1.6
            self.interval = min(self.interval, _MAX_INTERVAL)
            log.info("ai.stream_throttled", retry_after=exc.retry_after,
                     interval=round(self.interval, 1))
            await asyncio.sleep(min(exc.retry_after, 5))
            self._dirty.set()
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            self._broken = True
            log.info("ai.stream_push_failed", error=str(exc)[:120])

    async def _push(self, text: str) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _advance(self) -> None:
        """把回放进度往前推一段。收尾阶段（`_stopping`）直接到头。"""
        remaining = len(self._body) - self._revealed
        if remaining <= 0:
            return
        if self._stopping:
            self._revealed = len(self._body)
            return
        step = max(_REVEAL_MIN, int(remaining * self.reveal_ratio + 0.999))
        self._revealed = min(len(self._body), self._revealed + step)

    @property
    def _visible(self) -> str:
        return compose(self._notice, self._body[: self._revealed])

    @property
    def latest(self) -> str:
        """当前已经在屏幕上显示的内容。用户按停止时用来保留半截答案。"""
        return self._visible

    def stop(self) -> None:
        self._stopping = True
        self._dirty.set()


class DraftStreamer(_Pacer):
    """草稿流：客户端把同一 `draft_id` 的更新做成动画。只有私聊能用。

    `can_stop=True` 时 Telegram 自带「停止生成」按钮，按下会推
    `stopped_message_generation`（带 draft_id），handler 据此叫停。
    """

    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        draft_id: int,
        render: Callable[[str], str],
        interval: float = _DRAFT_INTERVAL,
    ) -> None:
        super().__init__(render=render, interval=interval, reveal_ratio=_DRAFT_REVEAL)
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id

    async def _push(self, text: str) -> None:
        await self.bot.send_message_draft(
            chat_id=self.chat_id, draft_id=self.draft_id, text=text, can_stop=True
        )

    async def settle(self) -> None:
        """收尾前把手里的内容推完，再等一个往返。

        草稿和正式消息不能并存：文档说「bot 发消息时草稿消失」，但那只在草稿
        请求全部落地之后才成立。第一版正是漏了这一步，用户看到「回了两条」。

        先 `_flush` 一次是因为 pump 可能还没跑过（引擎很快，或者刚创建就被叫停）——
        不推的话草稿一版都没出现，用户只看到最后那条正式消息。
        """
        self.stop()
        with contextlib.suppress(TelegramBadRequest, TelegramRetryAfter):
            await self._flush()
        await asyncio.sleep(_SETTLE)


class EditStreamer(_Pacer):
    """原地编辑：一条占位消息从头长到尾。群聊 / 草稿不可用时用。"""

    def __init__(
        self,
        *,
        bot: Bot,
        sent: Sent,
        render: Callable[[str], str],
        send_extra: SendExtra,
        interval: float = _EDIT_INTERVAL,
    ) -> None:
        super().__init__(render=render, interval=interval, reveal_ratio=_EDIT_REVEAL)
        self.bot = bot
        self.sent = sent
        self.send_extra = send_extra

    async def _push(self, text: str) -> None:
        await edit_text(self.bot, self.sent, text)

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
