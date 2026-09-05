"""ChannelSyncMiddleware 的集成测试：验证它真的挂在链上、真的不打扰用户指令。

与 test_sync.py 的分工：那里测「一条频道帖变成 Post 对不对」，这里测
「update 进了 Dispatcher 之后同步有没有被触发、有没有副作用」。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, PhotoSize, Update, User

from animebot.bot.middlewares import (
    AccessMiddleware,
    ChannelSyncMiddleware,
    ErrorBoundary,
    ObservabilityMiddleware,
    RateLimitMiddleware,
    TraceMiddleware,
)
from animebot.config import Settings
from animebot.core.registry import CommandRegistry, command, spec_of
from animebot.ingest.sync import ChannelSync
from animebot.storage.repo import PostRepo

USER_ID = 1001
NOW = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.UTC)
POST_TEXT = "中间件测试番\n中文名: 中间件测试番\n话数: 12\n标签：#测试"

# 真 PhotoSize：feed_update 会重新校验整个 Update，占位对象过不了。
PHOTO = [PhotoSize(file_id="f", file_unique_id="u", width=500, height=707)]


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


def channel_post_update(
    text: str, *, message_id: int = 5000, update_id: int = 1,
    chat_id: int | None = None, edited: bool = False,
) -> Update:
    """构造一条真·校验过的 Update。

    `feed_update` 会重新走一遍 pydantic 校验，所以这里不能用 model_construct
    塞占位对象。另外 aiogram 的 `edit_date` 字段类型是 int 而不是 datetime
    （`date` 是 datetime）—— 这个不一致正是 adapter 里 `_epoch()` 存在的理由。
    """
    msg = Message(
        message_id=message_id,
        date=NOW,
        edit_date=int(NOW.timestamp()) if edited else None,
        chat=Chat(id=chat_id if chat_id is not None else -1001702674582, type="channel"),
        text=text,
        photo=PHOTO,
    )
    key = "edited_channel_post" if edited else "channel_post"
    return Update(update_id=update_id, **{key: msg})


def private_message_update(text: str, *, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id, date=NOW,
            chat=Chat(id=USER_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="T"),
            text=text,
        ),
    )


def build_dp(
    registry: CommandRegistry,
    router: Router,
    settings: Settings,
    sync: ChannelSync,
) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["registry"] = registry
    for mw in (
        TraceMiddleware(registry),
        ErrorBoundary(),
        AccessMiddleware(registry, settings.admin_ids),
        RateLimitMiddleware(registry),
        ObservabilityMiddleware(settings),
        ChannelSyncMiddleware(sync),
    ):
        dp.update.outer_middleware(mw)
    dp.include_router(router)
    return dp


@pytest.fixture
def dp_with_sync(
    settings: Settings, repo: PostRepo, monkeypatch: pytest.MonkeyPatch
) -> tuple[Dispatcher, list[str]]:
    """带同步中间件的 Dispatcher + 记录 reply 的列表。"""
    replies: list[str] = []

    async def fake_reply(self: Message, text: str, **_kw: object) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)

    @command("echo")
    async def echo(message: Message) -> None:
        await message.reply("echoed")

    reg = CommandRegistry()
    from animebot.bot.wiring import bind_commands

    router = Router(name="test")
    spec = spec_of(echo)
    assert spec is not None
    bind_commands(router, [spec], reg, "test")

    dp = build_dp(reg, router, settings, ChannelSync(repo, settings))
    return dp, replies


async def test_channel_post_gets_indexed(
    dp_with_sync, fake_bot: Bot, repo: PostRepo
) -> None:
    dp, _ = dp_with_sync
    await dp.feed_update(bot=fake_bot, update=channel_post_update(POST_TEXT))
    stored = await repo.get(1702674582, 5000)
    assert stored is not None
    assert stored.title_cn == "中间件测试番"


async def test_edited_channel_post_gets_indexed(
    dp_with_sync, fake_bot: Bot, repo: PostRepo
) -> None:
    """edited_channel_post 和 channel_post 同等重要：真实数据里每条帖子都被编辑过。"""
    dp, _ = dp_with_sync
    await dp.feed_update(bot=fake_bot, update=channel_post_update(POST_TEXT))
    await dp.feed_update(
        bot=fake_bot,
        update=channel_post_update(
            POST_TEXT.replace("12", "24"), update_id=2, edited=True
        ),
    )
    stored = await repo.get(1702674582, 5000)
    assert stored is not None
    assert stored.episodes == "24"
    assert await repo.count() == 1


async def test_channel_post_gets_no_reply(
    dp_with_sync, fake_bot: Bot
) -> None:
    """绝不能在频道里回话。3000 人的频道不需要看到 bot 的自言自语。"""
    dp, replies = dp_with_sync
    await dp.feed_update(bot=fake_bot, update=channel_post_update(POST_TEXT))
    assert replies == []


async def test_sync_crash_does_not_break_chain(
    dp_with_sync, fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步是旁路。它炸了不能让 update 处理链断掉。"""
    dp, replies = dp_with_sync

    async def boom(self: ChannelSync, msg: Message, **_kw: object) -> None:
        raise RuntimeError("解析器炸了")

    monkeypatch.setattr(ChannelSync, "handle", boom, raising=True)
    await dp.feed_update(bot=fake_bot, update=channel_post_update(POST_TEXT))
    # 没抛出来就是对的；且没有在频道里发道歉消息
    assert replies == []


async def test_user_command_untouched_by_sync(
    dp_with_sync, fake_bot: Bot, repo: PostRepo
) -> None:
    """私聊指令不该被同步中间件干扰，也不该被写进索引。"""
    dp, replies = dp_with_sync
    await dp.feed_update(bot=fake_bot, update=private_message_update("/echo"))
    assert replies == ["echoed"]
    assert await repo.count(only_posts=False) == 0


async def test_wrong_channel_not_indexed(
    dp_with_sync, fake_bot: Bot, repo: PostRepo
) -> None:
    dp, _ = dp_with_sync
    await dp.feed_update(
        bot=fake_bot,
        update=channel_post_update(POST_TEXT, chat_id=-1001213081688),
    )
    assert await repo.count(only_posts=False) == 0


async def test_resolve_update_types_includes_channel_posts(
    settings: Settings, repo: PostRepo
) -> None:
    """同步靠中间件，而 resolve_used_update_types() 只扫 handler。

    不显式补上 channel_post，Telegram 就再也不推频道帖 —— 同步会永久静默
    失效，而且日志里一片安静。
    """
    from animebot.bot.app import resolve_update_types

    reg = CommandRegistry()
    router = Router(name="empty")

    @command("only_msg")
    async def only_msg(message: Message) -> None: ...

    from animebot.bot.wiring import bind_commands

    spec = spec_of(only_msg)
    assert spec is not None
    bind_commands(router, [spec], reg, "test")

    dp = build_dp(reg, router, settings, ChannelSync(repo, settings))
    types = resolve_update_types(dp)
    assert "channel_post" in types
    assert "edited_channel_post" in types
    assert "message" in types
