"""中间件：trace 注入、埋点、错误边界、限流。

按顺序串成一条链，所有指令共享。新指令不需要做任何事就自动获得全部保护
—— 这是把行为放中间件而不是装饰器的全部理由。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from ..config import Settings
from ..core.errors import BotError, PermissionDenied, RateLimited, UserError
from ..core.registry import CommandRegistry, CommandSpec
from ..ingest.sync import ChannelSync
from ..observability.context import RequestContext, reset_context, set_context
from ..observability.logging import get_logger
from ..observability.metrics import METRICS

log = get_logger("animebot.mw")

Next = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

_MAX_PAYLOAD_LOG = 120


def _extract_command(text: str | None) -> tuple[str | None, str]:
    """'/search 无职英雄@Bot' -> ('search', '无职英雄')"""
    if not text or not text.startswith("/"):
        return None, ""
    head, _, tail = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return (name or None), tail.strip()


class TraceMiddleware(BaseMiddleware):
    """最外层。每个 update 开一个 trace 上下文，之后所有日志自动带 trace_id。"""

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx = RequestContext(update_id=getattr(event, "update_id", None))

        inner = event.event if isinstance(event, Update) else event
        if isinstance(inner, Message):
            ctx.chat_id = inner.chat.id
            ctx.chat_type = inner.chat.type
            if inner.from_user:
                ctx.user_id = inner.from_user.id
                ctx.username = inner.from_user.username
            name, payload = _extract_command(inner.text or inner.caption)
            ctx.command = name
            ctx.payload = payload[:_MAX_PAYLOAD_LOG] or None
        elif isinstance(inner, CallbackQuery):
            ctx.user_id = inner.from_user.id if inner.from_user else None
            ctx.username = inner.from_user.username if inner.from_user else None
            if inner.message:
                ctx.chat_id = inner.message.chat.id
                ctx.chat_type = inner.message.chat.type
            ctx.command = "cb"
            ctx.payload = (inner.data or "")[:_MAX_PAYLOAD_LOG] or None

        if ctx.command and (spec := self._registry.get(ctx.command)):
            ctx.feature = spec.feature

        token = set_context(ctx)
        data["trace"] = ctx
        try:
            return await handler(event, data)
        finally:
            reset_context(token)


class ObservabilityMiddleware(BaseMiddleware):
    """计时 + 埋点 + 慢查询告警。放在错误边界内侧，异常也要被计时。"""

    def __init__(self, settings: Settings) -> None:
        self._slow_ms = settings.slow_command_ms

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: RequestContext | None = data.get("trace")
        label = ctx.command if ctx and ctx.command else "unknown"
        feature = ctx.feature if ctx and ctx.feature else "-"
        t0 = time.perf_counter()
        outcome = "ok"
        try:
            return await handler(event, data)
        except UserError:
            outcome = "user_error"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000
            METRICS.incr("handler.calls", command=label, feature=feature, outcome=outcome)
            METRICS.observe("handler.latency", ms, command=label, feature=feature)
            if ms > self._slow_ms:
                log.warning("handler.slow", command=label, elapsed_ms=round(ms, 1))
            else:
                log.debug("handler.done", command=label, outcome=outcome,
                          elapsed_ms=round(ms, 1))


class ErrorBoundary(BaseMiddleware):
    """唯一的兜底出口。

    UserError -> 原文给用户，info 日志。
    其它异常 -> 给用户一句带 trace_id 的道歉，exception 日志（含完整栈）。
    用户报「出错了，编号 a1b2c3d4」，直接 grep 日志就能定位。
    """

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: RequestContext | None = data.get("trace")
        tid = ctx.trace_id if ctx else "-"
        try:
            return await handler(event, data)
        except UserError as exc:
            log.info("handler.user_error", error=str(exc), error_type=type(exc).__name__)
            await _reply(event, exc.user_message)
        except BotError as exc:
            log.error("handler.bot_error", error=str(exc), error_type=type(exc).__name__)
            await _reply(event, f"{exc.user_message}\n\n错误编号: `{tid}`")
        except Exception as exc:
            log.exception("handler.crash", error_type=type(exc).__name__)
            METRICS.incr("handler.crash", error=type(exc).__name__)
            await _reply(
                event,
                f"内部错误，已记录。\n错误编号: `{tid}`\n把这个编号发给管理员可以帮助定位。",
            )
        return None


async def _reply(event: TelegramObject, text: str) -> None:
    """尽力回复。两条硬规矩：

    1. **绝不往频道里发消息。** channel_post 也是 Message，不挡住的话一次同步
       异常就会让 bot 在 3000 人的频道里公开发「内部错误，编号 xxx」。
    2. 回复本身失败也不能再抛 —— 那会变成 aiogram 的未处理异常，栈追踪里
       看不到原始错误。
    """
    inner = event.event if isinstance(event, Update) else event
    try:
        if isinstance(inner, Message):
            if inner.chat.type == ChatType.CHANNEL:
                log.warning("reply.suppressed_in_channel", text_head=text[:60])
                return
            await inner.reply(text)
        elif isinstance(inner, CallbackQuery):
            await inner.answer(text[:200], show_alert=True)
    except Exception:
        log.warning("reply.failed", text_head=text[:60])


class RateLimitMiddleware(BaseMiddleware):
    """按 (user_id, 指令) 滑动窗限流。限流参数来自 @command(rate=...)。

    进程内存实现：单实例部署够用。多实例时换 Redis，但接口不变。
    """

    def __init__(self, registry: CommandRegistry, default: tuple[int, float] | None = None) -> None:
        self._registry = registry
        self._default = default
        self._hits: dict[tuple[int, str], deque[float]] = defaultdict(deque)
        self._last_gc = time.monotonic()

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: RequestContext | None = data.get("trace")
        if ctx is None or ctx.command is None or ctx.user_id is None:
            return await handler(event, data)

        spec = self._registry.get(ctx.command)
        rate = spec.rate if spec and spec.rate else None
        if rate is None:
            return await handler(event, data)

        now = time.monotonic()
        self._gc(now)
        key = (ctx.user_id, spec.name)
        window = self._hits[key]
        cutoff = now - rate.per_seconds
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= rate.times:
            retry = window[0] + rate.per_seconds - now
            METRICS.incr("handler.rate_limited", command=spec.name)
            log.info("handler.rate_limited", command=spec.name, retry_after=round(retry, 1))
            raise RateLimited(max(retry, 0.5))

        window.append(now)
        return await handler(event, data)

    def _gc(self, now: float) -> None:
        """定期清空窗口，防止长期运行时 key 无限增长。"""
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        dead = [k for k, v in self._hits.items() if not v or now - v[-1] > 3600]
        for k in dead:
            del self._hits[k]


class AccessMiddleware(BaseMiddleware):
    """权限与场景校验：admin_only、group_allowed。"""

    def __init__(self, registry: CommandRegistry, admin_ids: tuple[int, ...]) -> None:
        self._registry = registry
        self._admins = frozenset(admin_ids)

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: RequestContext | None = data.get("trace")
        data["is_admin"] = bool(ctx and ctx.user_id in self._admins)

        if ctx is None or ctx.command is None:
            return await handler(event, data)
        spec: CommandSpec | None = self._registry.get(ctx.command)
        if spec is None:
            return await handler(event, data)

        if spec.admin_only and not data["is_admin"]:
            log.info("access.denied", command=spec.name, reason="admin_only")
            raise PermissionDenied()
        if not spec.group_allowed and ctx.chat_type in ("group", "supergroup"):
            raise UserError("这个指令只能在私聊里用")
        return await handler(event, data)


class ChannelSyncMiddleware(BaseMiddleware):
    """频道帖增量同步。

    做成中间件而不是 handler，因为：
      - channel_post 不是指令，没有 @command 元数据，走不了 wiring 那条路。
      - 它必须在所有 handler 之前跑完，否则别的 router 可能先命中并 return。
      - 频道帖不该经过限流和权限校验 —— 那两层是为用户指令设计的，
        频道自己发的帖子没有"用户"也没有"配额"。

    同步失败绝不能影响 update 继续往下走：一次解析异常不该让整条链断掉。
    """

    def __init__(self, sync: ChannelSync) -> None:
        self._sync = sync

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        inner = event.event if isinstance(event, Update) else event
        kind = _channel_post_kind(event, inner)
        if kind is not None and isinstance(inner, Message):
            try:
                await self._sync.handle(inner, kind=kind)
            except Exception:
                # 吞掉：同步是旁路，不能拖垮主链路。异常带完整栈进日志。
                log.exception("sync.crashed", message_id=inner.message_id)
        return await handler(event, data)


def _channel_post_kind(event: TelegramObject, inner: TelegramObject) -> str | None:
    """区分 new / edited。不是频道帖返回 None。

    优先看 Update 的字段名 —— 那是 Telegram 给出的权威分类。裸 Message
    （测试里直接喂的）退回看 edit_date。
    """
    if isinstance(event, Update):
        if event.channel_post is not None:
            return "new"
        if event.edited_channel_post is not None:
            return "edited"
        return None
    if isinstance(inner, Message) and inner.chat.type == ChatType.CHANNEL:
        return "edited" if inner.edit_date else "new"
    return None
