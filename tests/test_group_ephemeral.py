"""群聊里的检索入口：`@bot 关键词`、回复 bot、`/s`。

三条入口的投递都由 Telegram 的隐私模式规则保证（@提及 / 回复 / 明确指令都算
「针对 bot」），但**回复的可见性**是另一回事：群里的普通消息要 bot 有管理员
权限才能私密回复，否则只能公开。所以这里同时钉住两件事：

- 群消息没有被误当成搜索（闲聊灾难的防线，见 test_title_search.py）；
- 私密回复不可用时降级成公开回复，而不是让用户什么都收不到。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    EphemeralMessageParameters,
    Message,
    Update,
    User,
)

from animebot.bot import ui
from animebot.bot.wiring import bind_commands, collect_specs
from animebot.config import Settings
from animebot.core.registry import CommandRegistry
from animebot.features.search import handlers as search_handlers
from animebot.search.callbacks import QueryStore
from animebot.search.service import SearchService
from animebot.storage.repo import PostRepo

from .conftest import make_post

USER_ID = 7100
GROUP_ID = -1001234567890
BOT_ID = 42
BOT_USERNAME = "AnimeCoffeebot"
NOW = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=dt.UTC)


def make_bot() -> User:
    return User(id=BOT_ID, is_bot=True, first_name="Bot", username=BOT_USERNAME)


class Sent:
    """记录 reply / answer，包括 ephemeral 参数。"""

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.edit_texts: list[str] = []
        self.ephemeral_edits: list[dict[str, Any]] = []
        self.answer_raises: Exception | None = None


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    rec = Sent()

    async def fake_reply(self: Message, text: str, **kw: Any) -> None:
        rec.replies.append((text, kw))

    async def fake_answer(self: Message, text: str, **kw: Any) -> None:
        if rec.answer_raises is not None:
            raise rec.answer_raises
        rec.answers.append((text, kw))

    async def fake_edit(self: Message, text: str, **kw: Any) -> None:
        rec.edit_texts.append(text)

    async def fake_cb_answer(self: CallbackQuery, text: str | None = None, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    monkeypatch.setattr(Message, "answer", fake_answer, raising=True)
    monkeypatch.setattr(Message, "edit_text", fake_edit, raising=True)
    monkeypatch.setattr(CallbackQuery, "answer", fake_cb_answer, raising=True)
    return rec


@pytest.fixture
async def fake_bot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bot]:
    bot = Bot(token=f"{BOT_ID}:TEST-TOKEN-NOT-REAL")

    async def fake_me(_self: Bot) -> User:
        return make_bot()

    monkeypatch.setattr(Bot, "me", fake_me, raising=True)
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def dp(settings: Settings, repo: PostRepo) -> Dispatcher:
    d = Dispatcher()
    d["settings"] = settings
    d["repo"] = repo
    d["search"] = SearchService(repo, settings)
    d["query_store"] = QueryStore()
    reg = CommandRegistry()
    d["registry"] = reg

    router = Router(name="search")
    search_handlers.register_callbacks(router)
    search_handlers.register_plain_search(router)
    search_handlers.register_mention_search(router)
    bind_commands(router, collect_specs(search_handlers), reg, "search")
    d.include_router(router)
    return d


@pytest.fixture
async def seeded(repo: PostRepo) -> PostRepo:
    await repo.upsert_many([
        make_post(1, "青春猪头少年"),
        make_post(2, "无职转生"),
    ])
    return repo


def group_update(
    text: str | None,
    *,
    update_id: int = 1,
    reply_to: Message | None = None,
    ephemeral_id: int | None = None,
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=0 if ephemeral_id else update_id,
            date=NOW,
            chat=Chat(id=GROUP_ID, type="supergroup"),
            from_user=User(id=USER_ID, is_bot=False, first_name="T"),
            text=text,
            reply_to_message=reply_to,
            ephemeral_message_id=ephemeral_id,
        ),
    )


def bot_message(message_id: int = 500) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"),
        from_user=make_bot(),
        text="🔍 旧结果",
    )


# ---------------------------------------------------------------- @bot 入口


async def test_mention_with_keyword_searches(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(
        fake_bot, group_update(f"@{BOT_USERNAME} 青春")
    )
    assert sent.replies == [], "群里不该公开回复"
    assert len(sent.answers) == 1
    text, kw = sent.answers[0]
    assert "青春" in text
    assert kw["ephemeral_message_parameters"] == EphemeralMessageParameters(
        receiver_user_id=USER_ID
    )


async def test_mention_without_keyword_lists_commands(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """@bot 之后能看到「能干什么」，再决定搜什么。"""
    await dp.feed_update(fake_bot, group_update(f"@{BOT_USERNAME}"))
    text, kw = sent.answers[0]
    assert "/s" in text and "/c" in text
    assert "@AnimeCoffeebot" in text, "提示里要给出确切用法"
    assert "回复这条消息" in text, "回复 bot 是文档保证投递的路径，要写出来"
    assert kw["ephemeral_message_parameters"].receiver_user_id == USER_ID


async def test_mention_of_another_bot_ignored(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """@别的 bot 不该触发搜索 —— 会被当成关键词去搜，白跑一次。"""
    await dp.feed_update(fake_bot, group_update("@SomeOtherBot 青春"))
    assert sent.replies == []
    assert sent.answers == []


async def test_group_chatter_ignored(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """群里的普通闲聊绝不能当搜索 —— 这是这个功能最容易踩的坑。"""
    await dp.feed_update(fake_bot, group_update("青春这集真好看"))
    assert sent.replies == []
    assert sent.answers == []


# ---------------------------------------------------------------- 回复 bot


async def test_reply_to_bot_searches(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """回复 bot 的消息 = 搜索。给「@bot 看用法 → 直接回关键词」这条路径用。"""
    await dp.feed_update(
        fake_bot,
        group_update("青春", update_id=2, reply_to=bot_message()),
    )
    assert len(sent.answers) == 1
    assert "青春" in sent.answers[0][0]
    assert sent.answers[0][1]["ephemeral_message_parameters"].receiver_user_id == USER_ID


async def test_reply_to_human_ignored(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    human = Message(
        message_id=501, date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"),
        from_user=User(id=999, is_bot=False, first_name="Other"),
        text="上一句",
    )
    await dp.feed_update(
        fake_bot, group_update("青春", update_id=2, reply_to=human)
    )
    assert sent.replies == []
    assert sent.answers == []


# ---------------------------------------------------------------- 群里的指令


async def test_group_slash_command_replies_ephemerally(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """群里手打 /s（bot 是管理员或刚发过言时能收到）：结果同样私密。"""
    await dp.feed_update(fake_bot, group_update(f"/s@{BOT_USERNAME} 青春"))
    assert len(sent.answers) == 1
    assert "青春" in sent.answers[0][0]
    assert sent.answers[0][1]["ephemeral_message_parameters"].receiver_user_id == USER_ID


async def test_ephemeral_command_reply_stays_ephemeral(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """从 ephemeral 指令菜单发来的 /s：Message.reply 自己带 ephemeral 参数。

    这里不检查 aiogram 的自动补参（那是库的行为），只钉住「我们没有走公开回复」。
    """
    await dp.feed_update(
        fake_bot, group_update(f"/s@{BOT_USERNAME} 青春", ephemeral_id=77)
    )
    assert sent.answers == [], "ephemeral 入站消息不该再走公开 answer"
    assert len(sent.replies) == 1, "交给 Message.reply，由它补 ephemeral 参数"


async def test_private_plain_text_still_public(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """私聊行为不变：走 reply，不带任何 ephemeral 参数。"""
    update = Update(update_id=3, message=Message(
        message_id=3, date=NOW,
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text="青春",
    ))
    await dp.feed_update(fake_bot, update)
    assert len(sent.replies) == 1
    assert "ephemeral_message_parameters" not in sent.replies[0][1]


# ---------------------------------------------------------------- 降级


async def test_ephemeral_unavailable_falls_back_to_public(
    dp: Dispatcher, fake_bot: Bot, seeded: PostRepo, sent: Sent
) -> None:
    """bot 不是管理员、或客户端太老：私密回复失败，退回公开回复。

    宁可公开一条搜索结果，也不能让用户发了 @bot 之后什么都收不到。
    """
    sent.answer_raises = TelegramBadRequest(
        method=SendMessage(chat_id=GROUP_ID, text="x"),
        message="Bad Request: method is not available",
    )
    await dp.feed_update(fake_bot, group_update(f"@{BOT_USERNAME} 青春"))
    assert len(sent.replies) == 1
    assert "青春" in sent.replies[0][0]


# ---------------------------------------------------------------- 整套装配


async def test_build_app_wires_group_mention_end_to_end(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, sent: Sent, fake_bot: Bot
) -> None:
    """整套装配：真实中间件链 + DI，群里 @bot 能搜到、且回复是 ephemeral。

    单测里手工拼的 Dispatcher 会漏掉「registry / is_admin 有没有真的注入」这类
    问题 —— 那两个参数来自容器和 AccessMiddleware，不是 handler 自己能造的。

    `fake_bot` 夹具替换了 `Bot.me`，所以 `_MentionsMe` 不打网络。
    """
    from pydantic import SecretStr

    from animebot.bot.app import build_app

    monkeypatch.setattr("animebot.bot.app.setup_logging", lambda **_kw: None)
    cfg = settings.model_copy(update={"bot_token": SecretStr(f"{BOT_ID}:TEST-TOKEN-NOT-REAL")})
    app = await build_app(cfg)
    try:
        assert app.dp is not None and app.bot is not None
        app.bot._me = make_bot()   # 避免 getMe 真打 Telegram
        await app.container.get("repo").upsert_many([make_post(1, "青春猪头少年")])

        # 带关键词：搜到东西
        await app.dp.feed_update(fake_bot, group_update(f"@{BOT_USERNAME} 青春"))
        assert len(sent.answers) == 1
        assert "青春" in sent.answers[0][0]
        assert sent.answers[0][1]["ephemeral_message_parameters"].receiver_user_id == USER_ID

        # 不带关键词：列出指令（registry 注入成功才有内容）
        await app.dp.feed_update(
            fake_bot, group_update(f"@{BOT_USERNAME}", update_id=2)
        )
        listing = sent.answers[1][0]
        assert "/s" in listing and "/c" in listing
        assert "/ping" not in listing, "普通用户菜单里不该出现管理员指令"
    finally:
        await app.aclose()


async def test_ui_reply_builds_real_ephemeral_send_message(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：必须走 `Message.answer`，不能走 `Message.reply`。

    `reply()` 内部已经把 `ephemeral_message_parameters` 填成
    `as_ephemeral_message_parameters()`（普通消息上是 None），再从 kwargs 传一遍
    会直接 `TypeError: got multiple values for keyword argument` —— 线上每次群里
    搜都崩。上面的测试把两个方法都换成了假货，抓不到这个，所以这里用真对象。
    """
    calls: list[object] = []

    async def fake_call(self: Bot, method: object, **_kw: object) -> object:
        calls.append(method)
        return True

    monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

    msg = Message(
        message_id=5,
        date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text="@AnimeCoffeebot 青春",
    ).as_(fake_bot)

    await ui.reply(msg, "结果")

    sends = [c for c in calls if type(c).__name__ == "SendMessage"]
    assert len(sends) == 1
    send = sends[0]
    assert send.chat_id == GROUP_ID
    assert send.ephemeral_message_parameters == EphemeralMessageParameters(
        receiver_user_id=USER_ID
    )
    assert send.reply_parameters is None, "私密回复不该挂在群消息的回复链上"


async def test_ui_reply_private_has_no_ephemeral_params(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    async def fake_call(self: Bot, method: object, **_kw: object) -> object:
        calls.append(method)
        return True

    monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

    msg = Message(
        message_id=6,
        date=NOW,
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text="青春",
    ).as_(fake_bot)

    await ui.reply(msg, "结果")

    send = next(c for c in calls if type(c).__name__ == "SendMessage")
    assert send.ephemeral_message_parameters is None


# ---------------------------------------------------------------- safe_edit


async def test_safe_edit_ephemeral_uses_dedicated_method(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ephemeral 消息的 message_id 是 0，只能走 editEphemeralMessageText。"""
    calls: list[object] = []

    async def fake_call(self: Bot, method: object, **_kw: object) -> object:
        calls.append(method)
        return True

    monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

    msg = Message(
        message_id=0,
        date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text="旧结果",
        ephemeral_message_id=77,
        receiver_user=User(id=USER_ID, is_bot=False, first_name="T"),
    ).as_(fake_bot)
    cb = CallbackQuery(
        id="cb1",
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci",
        data="s:1:l:青春",
        message=msg,
    ).as_(fake_bot)

    await ui.safe_edit(cb, "新一页")

    edits = [c for c in calls if type(c).__name__ == "EditEphemeralMessageText"]
    assert len(edits) == 1, "ephemeral 消息不能用 editMessageText"
    method = edits[0]
    assert method.chat_id == GROUP_ID
    assert method.receiver_user_id == USER_ID
    assert method.ephemeral_message_id == 77
    assert method.text == "新一页"


async def test_safe_edit_normal_message_unchanged(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    async def fake_call(self: Bot, method: object, **_kw: object) -> object:
        calls.append(method)
        return True

    monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

    msg = Message(
        message_id=100, date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"), text="旧",
    ).as_(fake_bot)
    cb = CallbackQuery(
        id="cb2",
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci",
        data="s:1:l:青春",
        message=msg,
    ).as_(fake_bot)

    await ui.safe_edit(cb, "新")

    assert [type(c).__name__ for c in calls if type(c).__name__.startswith("Edit")] == [
        "EditMessageText"
    ]


async def test_safe_edit_not_modified_was_never_a_break(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：普通消息上重复点同一页仍然吞掉 not modified。"""

    async def fake_call(self: Bot, method: object, **_kw: object) -> object:
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")

    monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

    msg = Message(
        message_id=100, date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup"), text="旧",
    ).as_(fake_bot)
    cb = CallbackQuery(
        id="cb3",
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci",
        data="s:1:l:青春",
        message=msg,
    ).as_(fake_bot)

    await ui.safe_edit(cb, "旧")   # 不该抛
