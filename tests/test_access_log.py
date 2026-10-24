"""访问日志：每个被处理的交互一条 INFO，带上业务字段；polling 的瞬时网络错误降级。

为什么要测日志：线上排查只有 `journalctl` 一个窗口。这条线断了（字段丢了、
级别错了）不会有任何报错，只会在出事那天发现「日志里什么都没有」。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from structlog.testing import capture_logs

from animebot.bot.middlewares import (
    AccessMiddleware,
    ErrorBoundary,
    ObservabilityMiddleware,
    RateLimitMiddleware,
    TraceMiddleware,
)
from animebot.bot.wiring import bind_commands
from animebot.config import Settings
from animebot.core.errors import NotFound
from animebot.core.registry import CommandRegistry, command, spec_of
from animebot.observability.context import bind
from animebot.observability.logging import _inject_trace, _TransientPollingFilter
from animebot.observability.metrics import METRICS

USER_ID = 9001
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)


@command("probe")
async def cmd_probe(message: Message) -> None:
    bind(scope="manga", query="进击", result_count=3)
    await message.reply("ok")


@command("missing")
async def cmd_missing(message: Message) -> None:
    raise NotFound("这部漫画")


async def on_cb(callback: CallbackQuery) -> None:
    bind(feature="manga")
    await callback.answer()


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture(autouse=True)
def quiet_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(Message, "reply", noop, raising=True)
    monkeypatch.setattr(CallbackQuery, "answer", noop, raising=True)


def build_dp(settings: Settings) -> Dispatcher:
    reg = CommandRegistry()
    router = Router(name="probe")
    bind_commands(router, [spec_of(cmd_probe), spec_of(cmd_missing)], reg, "manga")  # type: ignore[list-item]
    router.callback_query.register(on_cb, F.data == "cx")
    dp = Dispatcher()
    for mw in (
        TraceMiddleware(reg),
        ErrorBoundary(),
        AccessMiddleware(reg, ()),
        RateLimitMiddleware(reg),
        ObservabilityMiddleware(settings),
    ):
        dp.update.outer_middleware(mw)
    dp.include_router(router)
    return dp


def text_update(text: str, *, chat_type: str = "private") -> Update:
    return Update(update_id=1, message=Message(
        message_id=1, date=NOW,
        chat=Chat(id=USER_ID if chat_type == "private" else -100123, type=chat_type),
        from_user=User(id=USER_ID, is_bot=False, first_name="T", username="tester"),
        text=text,
    ))


def cb_update(data: str) -> Update:
    return Update(update_id=2, callback_query=CallbackQuery(
        id="cb", from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci", data=data,
        message=Message(message_id=5, date=NOW, chat=Chat(id=USER_ID, type="private"), text="x"),
    ))


def events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


async def test_one_info_line_per_command_with_business_fields(
    settings: Settings, fake_bot: Bot
) -> None:
    dp = build_dp(settings)
    with capture_logs(processors=[_inject_trace]) as logs:
        await dp.feed_update(fake_bot, text_update("/probe 进击的巨人"))
    done = events(logs, "handler.done")
    assert len(done) == 1, "一次交互必须正好一行访问日志"
    line = done[0]
    assert line["log_level"] == "info"
    assert (line["command"], line["feature"], line["outcome"]) == ("probe", "manga", "ok")
    assert line["payload"] == "进击的巨人"
    assert (line["scope"], line["query"], line["result_count"]) == ("manga", "进击", 3)
    assert line["user_id"] == USER_ID and line["username"] == "tester"
    assert len(line["trace_id"]) == 8
    assert isinstance(line["elapsed_ms"], float)


async def test_user_error_is_still_one_access_line(settings: Settings, fake_bot: Bot) -> None:
    dp = build_dp(settings)
    with capture_logs(processors=[_inject_trace]) as logs:
        await dp.feed_update(fake_bot, text_update("/missing"))
    done = events(logs, "handler.done")
    assert [e["outcome"] for e in done] == ["user_error"]
    assert events(logs, "handler.user_error"), "错误边界那条带原因的日志也要在"


async def test_unhandled_update_is_debug_only(settings: Settings, fake_bot: Bot) -> None:
    """群里没触发任何指令的消息不该在 INFO 里刷屏。"""
    dp = build_dp(settings)
    with capture_logs(processors=[_inject_trace]) as logs:
        await dp.feed_update(fake_bot, text_update("大家好", chat_type="supergroup"))
    assert events(logs, "handler.done") == []
    unhandled = events(logs, "handler.unhandled")
    assert [e["log_level"] for e in unhandled] == ["debug"]
    assert unhandled[0]["command"] == "message"


async def test_slow_request_is_one_warning(settings: Settings, fake_bot: Bot) -> None:
    dp = build_dp(settings.model_copy(update={"slow_command_ms": -1.0}))
    with capture_logs(processors=[_inject_trace]) as logs:
        await dp.feed_update(fake_bot, text_update("/probe x"))
    assert events(logs, "handler.done") == []
    slow = events(logs, "handler.slow")
    assert [e["log_level"] for e in slow] == ["warning"]
    assert slow[0]["query"] == "进击", "慢请求那一行也要带业务字段，才知道是哪次搜索慢"


async def test_callback_feature_comes_from_handler(settings: Settings, fake_bot: Bot) -> None:
    """回调没有指令名，归属只有 handler 知道。标签必须在 handler 之后取。"""
    METRICS.reset()
    dp = build_dp(settings)
    with capture_logs(processors=[_inject_trace]) as logs:
        await dp.feed_update(fake_bot, cb_update("cx"))
    assert events(logs, "handler.done")[0]["feature"] == "manga"
    assert METRICS.snapshot()["counters"]["handler.calls{command=cb,feature=manga,outcome=ok}"] == 1


# ---------------------------------------------------------------- polling 噪音


def polling_record(exc_name: str) -> logging.LogRecord:
    return logging.LogRecord(
        "aiogram.dispatcher", logging.ERROR, __file__, 1,
        "Failed to fetch updates - %s: %s", (exc_name, "boom"), None,
    )


@pytest.mark.parametrize(
    "exc_name", ["TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter"]
)
def test_transient_polling_errors_downgraded(exc_name: str) -> None:
    rec = polling_record(exc_name)
    assert _TransientPollingFilter().filter(rec) is True, "降级而不是吞掉"
    assert (rec.levelno, rec.levelname) == (logging.WARNING, "WARNING")


@pytest.mark.parametrize("exc_name", ["TelegramConflictError", "TelegramUnauthorizedError"])
def test_actionable_polling_errors_stay_error(exc_name: str) -> None:
    """409 和 token 失效要人来处理，不能跟着降级。"""
    rec = polling_record(exc_name)
    _TransientPollingFilter().filter(rec)
    assert rec.levelno == logging.ERROR


def test_other_records_untouched() -> None:
    rec = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                            "Something else - %s", ("TelegramNetworkError",), None)
    _TransientPollingFilter().filter(rec)
    assert rec.levelno == logging.ERROR
