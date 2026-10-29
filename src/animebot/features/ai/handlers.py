"""`/q` 指令：AI 对话入口。

分工：这个文件只管 Telegram 的事（占位消息、停止按钮、把长文分片发出去），
模型和工具循环在 chat.py，协议细节在 client.py。

**流式做法：先发一条占位消息，然后原地 edit 它。** 不用 `sendMessageDraft` ——
它画的是临时草稿，会和最终消息同时出现在聊天里（看起来像回了两遍），而且只
支持私聊。edit 方案一条消息从头长到尾，私聊和群里都成立。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from ..._util import command_arg
from ...bot.ui import discard, edit_text
from ...bot.ui import reply as ui_reply
from ...config import Settings
from ...core.errors import UsageError
from ...core.registry import command
from ...observability.context import bind
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from .chat import ChatEngine
from .client import Stopped
from .render import to_telegram_html
from .session import ConversationStore
from .stream import EditStreamer, split_text

log = get_logger("animebot.ai")

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
#: 编辑时传空键盘 = 摘掉按钮（传 None 是「不改」，按钮会留在消息上）。
_NO_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[])
_RESET_WORDS = frozenset({"reset", "clear", "重置", "新对话", "清空"})
_PLACEHOLDER = "🤔 正在思考…"

#: 正在跑的生成：短 id -> 取消信号。id 编进「停止」按钮的 callback_data。
_GENERATIONS: dict[str, asyncio.Event] = {}
_SEQ = itertools.count(1)


def _new_generation() -> tuple[str, asyncio.Event]:
    gid = f"{next(_SEQ):x}{secrets.token_hex(2)}"
    cancel = asyncio.Event()
    _GENERATIONS[gid] = cancel
    return gid, cancel


def _stop_keyboard(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏹ 停止", callback_data=f"qs:{gid}")]]
    )


@command(
    "q",
    desc="问 AI（会查 Bangumi / 联网 / 查频道）",
    usage="/q 进击的巨人在 bangumi 上评分多少？",
    aliases=("ask", "ai"),
    rate=(4, 60),
    long_help=(
        "带工具的知识问答。它会自己决定查 Bangumi（搜条目 / 看详情 / 列榜单）、"
        "联网搜索还是翻频道索引，答案边生成边显示在同一条消息里。\n\n"
        "会记住上下文，追问不用重复背景。/q 重置 开新对话。\n\n"
        "例子:\n"
        "/q 进击的巨人在 bangumi 上评分多少？\n"
        "/q 春物评分多少？讲了什么\n"
        "/q 2026 年有什么穿越番推荐\n"
        "/q 近十年评分最高的动画\n"
        "/q 频道里有葬送的芙莉莲吗"
    ),
)
async def cmd_q(
    message: Message,
    engine: ChatEngine,
    conversations: ConversationStore,
    settings: Settings,
) -> None:
    arg = command_arg(message)
    key = (message.chat.id, message.from_user.id if message.from_user else 0)
    bind(feature="ai")

    if arg.lower() in _RESET_WORDS:
        cleared = conversations.reset(key)
        bind(reset=cleared)
        await ui_reply(message, "🧹 已开新对话。" if cleared else "🧹 本来就是新对话。")
        return
    if not arg:
        raise UsageError("问什么？", "/q 进击的巨人在 bangumi 上评分多少？")

    if not settings.ai_api_key.get_secret_value():
        raise UsageError(
            "AI 还没配好：管理员需要在 .env 里填 ANIMEBOT_AI_API_KEY 后重启。"
        )

    bind(query=arg[:80], model=settings.ai_model)
    conversation = conversations.get(key)
    gid, cancel = _new_generation()
    streamer: EditStreamer | None = None
    pump_task: asyncio.Task[None] | None = None

    try:
        sent = await ui_reply(
            message,
            _PLACEHOLDER,
            reply_markup=_stop_keyboard(gid),
            link_preview_options=_NO_PREVIEW,
        )
        if sent is not None and message.bot is not None:
            streamer = EditStreamer(
                bot=message.bot,
                sent=sent,
                render=to_telegram_html,
                send_extra=lambda html, **kw: ui_reply(
                    message, html, link_preview_options=_NO_PREVIEW, **kw
                ),
            )
            pump_task = asyncio.create_task(streamer.pump())

        def on_text(so_far: str) -> None:
            if streamer is not None:
                streamer.on_text(so_far)

        def on_notice(text: str) -> None:
            if streamer is not None:
                streamer.set_notice(text)

        result = await engine.answer(
            conversation, arg, on_text=on_text, on_notice=on_notice, cancel=cancel
        )
    except Stopped:
        METRICS.incr("ai.stopped")
        bind(stopped=True)
        if streamer is not None:
            await _stop_pump(streamer, pump_task)
            pump_task = None
            partial = streamer.latest.strip()
            note = "⏹ 已停止生成。"
            with contextlib.suppress(TelegramBadRequest):
                await streamer.finish(
                    f"{note}\n\n{partial}" if partial else note,
                    reply_markup=_NO_BUTTONS,
                )
        return
    except Exception:
        # 删掉占位消息，把错误交回 ErrorBoundary —— 它本来就会发一条带 trace_id
        # 的说明。留着占位就成了两条消息，正是这次要修的问题。
        if streamer is not None:
            await _stop_pump(streamer, pump_task)
            pump_task = None
            await discard(streamer.bot, streamer.sent)
        raise
    finally:
        await _stop_pump(streamer, pump_task)
        _GENERATIONS.pop(gid, None)

    METRICS.incr("ai.answer", found=bool(result.text))
    METRICS.observe("ai.tool_calls", result.tool_calls)
    bind(
        result_count=len(result.text),
        tool_calls=result.tool_calls,
        rounds=result.rounds,
        stop_reason=result.stop_reason,
    )

    if streamer is None:
        # 拿不到消息句柄（极端的 API 异常）：退化成一次性发送，功能不丢。
        for chunk in split_text(result.text):
            await ui_reply(
                message, to_telegram_html(chunk), link_preview_options=_NO_PREVIEW
            )
        return

    try:
        await streamer.finish(result.text, reply_markup=_NO_BUTTONS)
    except TelegramBadRequest as exc:
        if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
            raise
        # 模型写出了我们没转干净的标签 —— 退成纯文本，仍然编辑同一条消息。
        METRICS.incr("ai.html_fallback")
        log.info("ai.html_fallback", error=str(exc)[:120])
        await streamer.finish_plain(result.text, reply_markup=_NO_BUTTONS)


async def _stop_pump(streamer: EditStreamer | None, task: asyncio.Task[None] | None) -> None:
    if streamer is not None:
        streamer.stop()
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def on_stop(callback: CallbackQuery) -> None:
    """用户点了「⏹ 停止」。按 callback_data 里的 id 找回取消信号。"""
    bind(feature="ai", stopped=True)
    gid = (callback.data or "").removeprefix("qs:")
    cancel = _GENERATIONS.get(gid)
    if cancel is not None:
        cancel.set()
        METRICS.incr("ai.stop_requested")
    await callback.answer("正在停止…" if cancel is not None else "已经结束了")


def register_callbacks(router: Router) -> None:
    router.callback_query.register(on_stop, F.data.startswith("qs:"))


__all__ = ["cmd_q", "discard", "edit_text", "on_stop", "register_callbacks"]
