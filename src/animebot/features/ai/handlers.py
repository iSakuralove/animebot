"""`/q` 指令：AI 对话入口。

分工：这个文件只管 Telegram 的事（流式落地、停止、把长文发出去），模型和
工具循环在 chat.py，协议细节在 client.py。

流式的两种落地方式在 stream.py 里，这里只做选择：

- **私聊** → 草稿流（`sendMessageDraft`）。客户端会把同一 `draft_id` 的更新
  做成动画，看起来是顺滑地在打字。收尾必须再发一条正式消息（草稿是临时的），
  而且发之前要等一个往返，否则在途的草稿会和正式消息并存。
- **群聊** → 原地编辑（草稿不支持群）。先发一条占位消息并带上「停止」按钮，
  再反复 edit 它。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import secrets
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageGenerationStopped,
)

from ..._util import command_arg
from ...bot.ui import discard
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
from .stream import DraftStreamer, EditStreamer, split_text

log = get_logger("animebot.ai")

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
#: 编辑时传空键盘 = 摘掉按钮（传 None 是「不改」，按钮会留在消息上）。
_NO_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[])
_RESET_WORDS = frozenset({"reset", "clear", "重置", "新对话", "清空"})
_PLACEHOLDER = "🤔 正在思考…"

@dataclass(slots=True)
class _Generation:
    """一次正在跑的生成。`gid` 编进停止按钮的 callback_data。"""

    cancel: asyncio.Event
    requester_id: int | None     # 群里只让提问者停，别人点了没反应
    public: bool                 # 公开回复时，停止按钮要挡住旁观者


#: 正在跑的生成：短 id -> 生成。编辑模式用「⏹ 停止」按钮的 callback_data 找回；
#: 草稿模式用 Telegram 自带的停止按钮，它只给 draft_id，所以另存一张反向表。
_GENERATIONS: dict[str, _Generation] = {}
_BY_DRAFT: dict[int, str] = {}
_SEQ = itertools.count(1)


def _new_generation(
    requester_id: int | None = None, *, public: bool = False
) -> tuple[str, _Generation]:
    gid = f"{next(_SEQ):x}{secrets.token_hex(2)}"
    gen = _Generation(asyncio.Event(), requester_id, public)
    _GENERATIONS[gid] = gen
    return gid, gen


def _new_draft_id(gid: str) -> int:
    """草稿 id：非零、同一 chat 内唯一。顺手记住它属于哪个生成。"""
    draft_id = int(gid, 16) & 0x7FFFFFFF or 1
    _BY_DRAFT[draft_id] = gid
    return draft_id


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
    # 群里的答案是公开的，问题也必须公开 —— 否则群里只看到 bot 在自说自话。
    group_ephemeral=False,
    long_help=(
        "带工具的知识问答。它会自己决定查 Bangumi（搜条目 / 看详情 / 列榜单）、"
        "联网搜索还是翻频道索引，答案边生成边显示出来。\n\n"
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

    # 群里的 /q 公开回答：这是全群都有用的信息，藏起来没道理。
    # 搜索结果相反 —— 那是某个人在找东西，公开等于替他刷屏（见 ADR-0015）。
    public = message.chat.type in ("group", "supergroup")
    requester = message.from_user.id if message.from_user else None

    bind(query=arg[:80], model=settings.ai_model, public=public)
    conversation = conversations.get(key)
    gid, gen = _new_generation(requester, public=public)
    cancel = gen.cancel
    streamer: DraftStreamer | EditStreamer | None = None
    pump_task: asyncio.Task[None] | None = None
    draft_id: int | None = None
    placeholder = None

    try:
        streamer, placeholder, draft_id = await _make_streamer(
            message, gid, settings, public=public
        )
        if streamer is not None:
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
            await _emit_stop(message, streamer, public=public)
        return
    except Exception:
        # 删掉占位消息、让在途草稿落地，把错误交回 ErrorBoundary —— 它本来就会
        # 发一条带 trace_id 的说明。留着占位就成了两条消息。
        if streamer is not None:
            await _stop_pump(streamer, pump_task)
            pump_task = None
            await _cleanup(streamer, placeholder)
        raise
    finally:
        await _stop_pump(streamer, pump_task)
        _GENERATIONS.pop(gid, None)
        if draft_id is not None:
            _BY_DRAFT.pop(draft_id, None)

    METRICS.incr("ai.answer", found=bool(result.text))
    METRICS.observe("ai.tool_calls", result.tool_calls)
    bind(
        result_count=len(result.text),
        tool_calls=result.tool_calls,
        rounds=result.rounds,
        stop_reason=result.stop_reason,
    )

    await _deliver(message, streamer, result.text, public=public)


async def _make_streamer(
    message: Message, gid: str, settings: Settings, *, public: bool = False
) -> tuple[DraftStreamer | EditStreamer | None, Message | None, int | None]:
    """按场景选流式落地方式，返回 (streamer, 占位消息, 草稿 id)。"""
    if message.bot is None:
        return None, None, None

    if settings.ai_stream_mode == "draft" and message.chat.type == "private":
        draft_id = _new_draft_id(gid)
        # 草稿只能「发出去才看得到」：先推一版「正在思考」，否则前几秒屏幕上
        # 什么都没有，用户会以为没点上。
        streamer = DraftStreamer(
            bot=message.bot,
            chat_id=message.chat.id,
            draft_id=draft_id,
            render=to_telegram_html,
        )
        streamer.set_notice(_PLACEHOLDER)
        return streamer, None, draft_id

    # 群聊 / 关掉草稿：发一条占位消息，之后原地编辑它。
    sent = await ui_reply(
        message,
        _PLACEHOLDER,
        public=public,
        reply_markup=_stop_keyboard(gid),
        link_preview_options=_NO_PREVIEW,
    )
    if sent is None:
        return None, None, None
    streamer = EditStreamer(
        bot=message.bot,
        sent=sent,
        render=to_telegram_html,
        send_extra=lambda html, **kw: ui_reply(
            message, html, public=public, link_preview_options=_NO_PREVIEW, **kw
        ),
    )
    return streamer, sent, None


async def _deliver(
    message: Message,
    streamer: DraftStreamer | EditStreamer | None,
    text: str,
    *,
    public: bool = False,
) -> None:
    """把最终回答发出去。草稿模式要先把草稿收掉，编辑模式改占位消息。"""
    if isinstance(streamer, DraftStreamer):
        await streamer.settle()
        bind(stream_frames=streamer.frames)
        for index, chunk in enumerate(split_text(text)):
            rich = to_telegram_html(chunk)
            try:
                await ui_reply(
                    message, rich, public=public, link_preview_options=_NO_PREVIEW,
                    reply_markup=_NO_BUTTONS if index == 0 else None,
                )
            except TelegramBadRequest as exc:
                if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                    raise
                METRICS.incr("ai.html_fallback")
                log.info("ai.html_fallback", error=str(exc)[:120])
                await ui_reply(
                    message, chunk, public=public,
                    link_preview_options=_NO_PREVIEW, parse_mode=None,
                )
        return

    if not isinstance(streamer, EditStreamer):
        # 拿不到任何落地手段（极端异常）：一次性发送，功能不丢。
        for chunk in split_text(text):
            await ui_reply(
                message, to_telegram_html(chunk), public=public,
                link_preview_options=_NO_PREVIEW,
            )
        return

    bind(stream_frames=streamer.frames)
    try:
        await streamer.finish(text, reply_markup=_NO_BUTTONS)
    except TelegramBadRequest as exc:
        if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
            raise
        METRICS.incr("ai.html_fallback")
        log.info("ai.html_fallback", error=str(exc)[:120])
        await streamer.finish_plain(text, reply_markup=_NO_BUTTONS)


async def _emit_stop(
    message: Message, streamer: DraftStreamer | EditStreamer, *, public: bool = False
) -> None:
    """用户喊停：保留已经生成的部分，再补一句说明。"""
    partial = streamer.latest.strip()
    note = "⏹ 已停止生成。"
    body = f"{note}\n\n{partial}" if partial else note
    with contextlib.suppress(TelegramBadRequest):
        if isinstance(streamer, DraftStreamer):
            await streamer.settle()
            await ui_reply(message, to_telegram_html(body), public=public,
                           link_preview_options=_NO_PREVIEW)
        else:
            await streamer.finish(body, reply_markup=_NO_BUTTONS)


async def _cleanup(
    streamer: DraftStreamer | EditStreamer,
    placeholder: Message | None,
) -> None:
    if isinstance(streamer, DraftStreamer):
        await streamer.settle()      # 让在途草稿落地，别再冒出来
        return
    if placeholder is not None:
        await discard(streamer.bot, streamer.sent)


async def _stop_pump(
    streamer: DraftStreamer | EditStreamer | None,
    task: asyncio.Task[None] | None,
) -> None:
    if streamer is not None:
        streamer.stop()
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def on_stop(callback: CallbackQuery) -> None:
    """群里那个「⏹ 停止」按钮。草稿模式没有这个按钮（Telegram 自带）。"""
    bind(feature="ai", stopped=True)
    gid = (callback.data or "").removeprefix("qs:")
    gen = _GENERATIONS.get(gid)
    if gen is None:
        await callback.answer("已经结束了")
        return
    # 公开回答时按钮全群可见。别人点了不给他停 —— 那是别人的提问，
    # 但也不装作成功，明确告诉他。
    if gen.public and gen.requester_id and callback.from_user.id != gen.requester_id:
        await callback.answer("只有提问的人能停止", show_alert=True)
        return
    gen.cancel.set()
    METRICS.incr("ai.stop_requested")
    await callback.answer("正在停止…")


async def on_generation_stopped(event: MessageGenerationStopped) -> None:
    """草稿模式下用户按了 Telegram 自带的「停止生成」。更新里只有 draft_id。"""
    bind(feature="ai", stopped=True)
    gid = _BY_DRAFT.get(event.draft_id)
    gen = _GENERATIONS.get(gid) if gid else None
    if gen is not None:
        gen.cancel.set()
        METRICS.incr("ai.stop_requested")
        log.info("ai.stop_from_draft", draft_id=event.draft_id)


def register_callbacks(router: Router) -> None:
    router.callback_query.register(on_stop, F.data.startswith("qs:"))
    router.stopped_message_generation.register(on_generation_stopped, F)


__all__ = ["cmd_q", "on_generation_stopped", "on_stop", "register_callbacks"]
