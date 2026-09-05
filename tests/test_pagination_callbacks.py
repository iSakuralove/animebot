"""翻页回调的集成测试：验证按钮真的能被点、真的会编辑消息。

与 test_pagination.py 的分工：那里测「页数算得对不对、按钮长什么样」，
这里测「callback_query 进了 Dispatcher 之后有没有走到 handler、有没有正确
处理 Telegram 的报错」。

这一层最容易漏的是**注册顺序**：翻页的前缀是 `s:`/`t:`，详情是 `p:`。
过滤器写错的话点翻页会出详情，或者两个都不响应 —— 而这两种错都不抛异常。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)

from animebot.config import Settings
from animebot.core.registry import CommandRegistry
from animebot.features.search import handlers as search_handlers
from animebot.search.callbacks import NOOP, QueryStore, encode_page
from animebot.search.service import SearchService
from animebot.storage.repo import PostRepo

from .conftest import CHANNEL_ID, make_post

USER_ID = 7001
NOW = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.UTC)


def bad_request(message: str) -> TelegramBadRequest:
    """造一个 TelegramBadRequest。

    它要求一个具体的 TelegramMethod（抽象基类不能实例化），随便挑一个真方法
    就行 —— 测的是 handler 怎么根据 message 文本分流，method 是什么无关。
    """
    return TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=1, text="x"),
        message=message,
    )


class Edits:
    """记录 edit_text / answer / alert，而不是真发请求。"""

    def __init__(self) -> None:
        self.edits: list[str] = []
        self.markups: list[InlineKeyboardMarkup | None] = []
        self.answers: list[str] = []
        self.alerts: list[str] = []
        self.new_messages: list[str] = []
        self.edit_raises: Exception | None = None


@pytest.fixture
def edits(monkeypatch: pytest.MonkeyPatch) -> Edits:
    rec = Edits()

    async def fake_edit(self: Message, text: str, **kw: object) -> None:
        if rec.edit_raises is not None:
            raise rec.edit_raises
        rec.edits.append(text)
        rec.markups.append(kw.get("reply_markup"))  # type: ignore[arg-type]

    async def fake_answer_msg(self: Message, text: str, **kw: object) -> None:
        rec.new_messages.append(text)

    async def fake_cb_answer(
        self: CallbackQuery, text: str | None = None, **kw: object
    ) -> None:
        if kw.get("show_alert"):
            rec.alerts.append(text or "")
        else:
            rec.answers.append(text or "")

    monkeypatch.setattr(Message, "edit_text", fake_edit, raising=True)
    monkeypatch.setattr(Message, "answer", fake_answer_msg, raising=True)
    monkeypatch.setattr(CallbackQuery, "answer", fake_cb_answer, raising=True)
    return rec


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def store() -> QueryStore:
    return QueryStore()


@pytest.fixture
def dp(
    settings: Settings, repo: PostRepo, store: QueryStore
) -> Dispatcher:
    """只挂 search 模块的回调，不要中间件 —— 这里测的是路由和 handler。"""
    from aiogram import Router

    d = Dispatcher()
    d["settings"] = settings
    d["repo"] = repo
    d["search"] = SearchService(repo, settings)
    d["query_store"] = store
    d["registry"] = CommandRegistry()

    router = Router(name="search-cb")
    search_handlers.register_callbacks(router)
    d.include_router(router)
    return d


def cb_update(data: str, *, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb{update_id}",
            from_user=User(id=USER_ID, is_bot=False, first_name="T"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=100,
                date=NOW,
                chat=Chat(id=USER_ID, type="private"),
                text="旧的结果列表",
            ),
        ),
    )


@pytest.fixture
async def seeded(repo: PostRepo) -> PostRepo:
    await repo.upsert_many([make_post(i, f"测试番剧{i:03d}") for i in range(1, 26)])
    return repo


# ---------------------------------------------------------------- 翻页


async def test_next_page_edits_message(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    """翻页原地编辑，不发新消息 —— 否则聊天记录被同一次搜索刷满。"""
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(1, "测试番剧", store)))
    assert len(edits.edits) == 1
    assert edits.new_messages == []
    assert "第 2/4 页" in edits.edits[0]
    assert "9-16" in edits.edits[0]


async def test_keyboard_updated_on_turn(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    """光换文字不换键盘的话，翻到末页后「下一页」还是亮的。"""
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(3, "测试番剧", store)))
    kb = edits.markups[0]
    assert kb is not None
    assert [b.text for b in kb.inline_keyboard[-1]] == ["⏮", "◀", "4/4", "·", "·"]


async def test_jump_to_last(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(3, "测试番剧", store)))
    assert "25" in edits.edits[0]


async def test_jump_to_first(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(0, "测试番剧", store)))
    assert "第 1/4 页" in edits.edits[0]


async def test_out_of_range_page_clamps(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    """按钮是旧的、结果集变小了 —— 不该回一个空列表。"""
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(99, "测试番剧", store)))
    assert "第 4/4 页" in edits.edits[0]


async def test_long_query_via_token(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    long_q = "英雄王，为了穷尽武道而转生～而后，成为世界最强的见习骑士♀～ 测试番剧"
    data = encode_page(1, long_q, store)
    assert data.startswith("t:")
    await dp.feed_update(bot=fake_bot, update=cb_update(data))
    assert len(edits.edits) == 1


async def test_expired_token_tells_user(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    """token 过期要有明确提示，不能静默失败也不能搜错东西。"""
    await dp.feed_update(bot=fake_bot, update=cb_update("t:1:deadbeef"))
    assert edits.edits == []
    assert len(edits.alerts) == 1
    assert "过期" in edits.alerts[0]


async def test_not_modified_swallowed(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    """重复点同一页，Telegram 报 not modified。不是错误，不该冒泡。"""
    edits.edit_raises = bad_request("Bad Request: message is not modified")
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(1, "测试番剧", store)))
    assert edits.answers == [""], "回调没被 answer，客户端会一直转圈"


async def test_other_bad_request_propagates(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    """真的错误必须冒泡到 ErrorBoundary，不能和 not modified 一起吞掉。"""
    edits.edit_raises = bad_request("Bad Request: message to edit not found")
    with pytest.raises(TelegramBadRequest):
        await dp.feed_update(
            bot=fake_bot, update=cb_update(encode_page(1, "测试番剧", store))
        )


async def test_callback_always_answered(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(1, "测试番剧", store)))
    assert len(edits.answers) + len(edits.alerts) == 1


# ---------------------------------------------------------------- 路由隔离


async def test_noop_button_answered_silently(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    """页码和占位按钮必须 answer，否则客户端一直转圈。"""
    await dp.feed_update(bot=fake_bot, update=cb_update(NOOP))
    assert edits.edits == []
    assert edits.answers == [""]
    assert edits.alerts == []


async def test_detail_button_not_caught_by_pager(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    """点详情要发新消息，不能编辑掉结果列表。过滤器写错这两个会串。"""
    await dp.feed_update(bot=fake_bot, update=cb_update("p:5"))
    assert len(edits.new_messages) == 1
    assert "测试番剧005" in edits.new_messages[0]
    assert edits.edits == [], "详情把结果列表编辑掉了"


async def test_page_button_not_caught_by_detail(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, store: QueryStore, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update(encode_page(1, "测试番剧", store)))
    assert edits.new_messages == [], "翻页发了新消息而不是编辑"
    assert len(edits.edits) == 1


async def test_detail_missing_post(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update("p:999999"))
    assert len(edits.alerts) == 1
    assert "不在索引" in edits.alerts[0]


async def test_detail_malformed(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    await dp.feed_update(bot=fake_bot, update=cb_update("p:abc"))
    assert len(edits.alerts) == 1
    assert "失效" in edits.alerts[0]


async def test_detail_uses_configured_channel(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, edits: Edits
) -> None:
    """详情按 (channel_id, message_id) 取，channel_id 来自配置。"""
    await dp.feed_update(bot=fake_bot, update=cb_update("p:5"))
    assert str(CHANNEL_ID) in edits.new_messages[0] or "t.me/YXHMd/5" in edits.new_messages[0]
