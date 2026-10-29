"""`/q` 指令：AI 对话入口。

分工：这个文件只管 Telegram 的事（草稿流、停止按钮、把长文分片发出去），
模型和工具循环在 chat.py，协议细节在 client.py。

流式只在私聊做。`sendMessageDraft` 的 chat_id 参数文档写的是 private chat，
群里的回复本来就走 ephemeral，再套草稿没有意义（草稿没有 ephemeral 版本，
会把别人的搜索过程公开）。
"""

from __future__ import annotations

import asyncio
import contextlib
import random

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message, MessageGenerationStopped

from ..._util import command_arg
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
from .stream import DraftStreamer, split_text

log = get_logger("animebot.ai")

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_RESET_WORDS = frozenset({"reset", "clear", "重置", "新对话", "清空"})

#: draft_id -> 取消信号。用户点「停止生成」时 Telegram 推 stopped_message_generation，
#: 里面只有 draft_id，所以映射只能按它建。
_CANCELS: dict[int, asyncio.Event] = {}


def new_draft_id() -> int:
    """草稿 id 必须非零，且同一 chat 内同时只有一条在刷 —— 随机足够。"""
    return random.getrandbits(31) | 1


@command(
    "q",
    desc="问 AI（会查 Bangumi / 联网 / 查频道）",
    usage="/q 进击的巨人在 bangumi 上评分多少？",
    aliases=("ask", "ai"),
    rate=(4, 60),
    long_help=(
        "带工具的知识问答。它会自己决定查 Bangumi、联网搜索还是翻频道索引，"
        "答案是流式的（私聊里能看到逐字生成）。\n\n"
        "会记住上下文，追问不用重复背景。/q 重置 开新对话。\n\n"
        "例子:\n"
        "/q 进击的巨人在 bangumi 上评分多少？\n"
        "/q 频道里有葬送的芙莉莲吗\n"
        "/q 今年七月番里评分最高的是哪部"
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

    streamer = _make_streamer(message)
    pump_task: asyncio.Task[None] | None = None
    if streamer is not None:
        _CANCELS[streamer.draft_id] = cancel = asyncio.Event()
        pump_task = asyncio.create_task(streamer.pump())
    else:
        cancel = asyncio.Event()

    status = {"notice": ""}

    def on_text(so_far: str) -> None:
        if streamer is not None:
            streamer.on_text(_compose(status["notice"], so_far))

    def on_notice(text: str) -> None:
        status["notice"] = text
        if streamer is not None:
            streamer.push_now(_compose(text, ""))

    try:
        result = await engine.answer(
            conversation, arg, on_text=on_text, on_notice=on_notice, cancel=cancel
        )
    except Stopped:
        METRICS.incr("ai.stopped")
        bind(stopped=True)
        if streamer is not None:
            streamer.stop()
        await _retract(streamer)
        await ui_reply(message, "⏹ 已停止生成。")
        return
    finally:
        if streamer is not None:
            streamer.stop()
        if pump_task is not None:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
        if streamer is not None:
            _CANCELS.pop(streamer.draft_id, None)

    METRICS.incr("ai.answer", found=bool(result.text))
    METRICS.observe("ai.tool_calls", result.tool_calls)
    bind(
        result_count=len(result.text),
        tool_calls=result.tool_calls,
        rounds=result.rounds,
        stop_reason=result.stop_reason,
    )

    await _send_answer(message, result.text)


def _compose(notice: str, body: str) -> str:
    """草稿正文：状态提示一行 + 已生成的正文。"""
    body = body.strip()
    if notice and body:
        return f"{notice}\n\n{body}"
    return notice or body


def _make_streamer(message: Message) -> DraftStreamer | None:
    """只有私聊能刷草稿。群、频道返回 None，走「等完了一次性发」。"""
    if message.chat.type != "private" or message.bot is None:
        return None
    return DraftStreamer(
        bot=message.bot,
        chat_id=message.chat.id,
        draft_id=new_draft_id(),
        render=to_telegram_html,
    )


async def _retract(streamer: DraftStreamer | None) -> None:
    """停止生成后把草稿清掉 —— 否则残留的 30 秒预览和「已停止」并存，很怪。"""
    if streamer is None:
        return
    try:
        await streamer.bot.send_message_draft(
            chat_id=streamer.chat_id, draft_id=streamer.draft_id, text=""
        )
    except Exception:   # 清理失败不影响主流程
        log.debug("ai.draft_retract_failed")


async def _send_answer(message: Message, markdown: str) -> None:
    """发最终回答：HTML 优先，解析失败退回纯文本，超长自动分片。"""
    chunks = split_text(markdown)
    for chunk in chunks:
        rich = to_telegram_html(chunk)
        try:
            await ui_reply(message, rich, link_preview_options=_NO_PREVIEW)
        except TelegramBadRequest as exc:
            if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                raise
            # 模型写出了我们没转干净的标签 —— 退成纯文本，不能让用户收不到答案。
            METRICS.incr("ai.html_fallback")
            log.info("ai.html_fallback", error=str(exc)[:120])
            await ui_reply(
                message, chunk, link_preview_options=_NO_PREVIEW, parse_mode=None
            )


async def on_stopped(event: MessageGenerationStopped) -> None:
    """用户点了「停止生成」。按 draft_id 找回对应的协程并叫停。"""
    cancel = _CANCELS.get(event.draft_id)
    bind(feature="ai", stopped=True)
    if cancel is not None:
        cancel.set()
        METRICS.incr("ai.stop_requested")


def register_callbacks(router: Router) -> None:
    router.stopped_message_generation.register(on_stopped, F)
