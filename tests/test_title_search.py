"""私聊纯文本 = 标题搜索的路由与行为测试。

这个 handler 最大的风险是**过滤器串味**（和 test_pagination_callbacks.py 同源）：
- 装到群里 → 每句闲聊都被当搜索，灾难；
- 抢了指令 → /s、/check 全被纯文本 handler 吃掉；
- 遇到图片（无 text）→ 崩。

三种都不抛异常，只会让 bot 行为诡异，所以每条都断言「有没有触发搜索」。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from animebot.config import Settings
from animebot.features.search import handlers as search_handlers
from animebot.search.callbacks import QueryStore
from animebot.search.service import SearchService
from animebot.storage.repo import PostRepo

from .conftest import make_post

USER_ID = 7002
NOW = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.UTC)


class Replies:
    """记录 message.reply，而不是真发请求。"""

    def __init__(self) -> None:
        self.texts: list[str] = []


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> Replies:
    rec = Replies()

    async def fake_reply(self: Message, text: str, **kw: object) -> None:
        rec.texts.append(text)

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    return rec


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def dp(settings: Settings, repo: PostRepo) -> Dispatcher:
    """只挂纯文本 handler，测的就是它的过滤器。"""
    d = Dispatcher()
    d["settings"] = settings
    d["repo"] = repo
    d["search"] = SearchService(repo, settings)
    d["query_store"] = QueryStore()

    router = Router(name="plain-search")
    search_handlers.register_plain_search(router)
    d.include_router(router)
    return d


@pytest.fixture
async def seeded(repo: PostRepo) -> PostRepo:
    await repo.upsert_many([
        make_post(1, "青春猪头少年"),
        make_post(2, "无职转生"),
    ])
    return repo


def msg_update(
    text: str | None, *, chat_type: str = "private", update_id: int = 1
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=NOW,
            chat=Chat(id=USER_ID, type=chat_type),
            from_user=User(id=USER_ID, is_bot=False, first_name="T"),
            text=text,
        ),
    )


async def test_private_plain_text_triggers_search(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, replies: Replies
) -> None:
    """私聊里打「青春」= 标题搜索，回一条结果。"""
    await dp.feed_update(bot=fake_bot, update=msg_update("青春"))
    assert len(replies.texts) == 1
    # 命中词「青春」被 <b> 高亮，标题不再是连续子串，断言未高亮的尾部
    assert "猪头少年" in replies.texts[0]


async def test_group_plain_text_ignored(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, replies: Replies
) -> None:
    """群里纯文本无反应 —— 不注册这个 handler，闲聊不会被当搜索。"""
    await dp.feed_update(bot=fake_bot, update=msg_update("青春", chat_type="supergroup"))
    assert replies.texts == []


async def test_command_not_swallowed(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, replies: Replies
) -> None:
    """以 / 开头的指令不该被纯文本 handler 接走 —— 留给 Command 过滤器。"""
    await dp.feed_update(bot=fake_bot, update=msg_update("/s 青春"))
    assert replies.texts == []


async def test_no_text_message_ignored(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, replies: Replies
) -> None:
    """图片/贴纸（text=None）不该触发也不该崩 —— magic_filter 对 None 返回 falsy。"""
    await dp.feed_update(bot=fake_bot, update=msg_update(None))
    assert replies.texts == []


async def test_private_search_is_title_only(
    dp: Dispatcher, fake_bot: Bot, repo: PostRepo, replies: Replies
) -> None:
    """私聊纯文本走标题模式：只在正文提到的词不该命中（否则又回到混全文的老问题）。"""
    await repo.upsert_many([make_post(1, "某动画", raw_text="讲一个青春的故事")])
    await dp.feed_update(bot=fake_bot, update=msg_update("青春"))
    assert len(replies.texts) == 1
    # 标题模式零命中 → 走「没找到」空结果提示，而不是靠正文捞出来
    assert "没找到" in replies.texts[0]
