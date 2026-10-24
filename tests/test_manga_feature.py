"""/c 漫画检索：指令、落地页、翻页回调、展示层，以及整套装配。

与动漫搜索的测试同一个套路：手搓 Update 喂进 Dispatcher，只替换「发消息」
那一层。最该盯的是**串台**：漫画的翻页按钮被动漫模块接走（或反过来）不会
报错，只会让用户看到另一个频道的结果。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from pydantic import SecretStr

from animebot.bot.wiring import bind_commands, collect_specs
from animebot.config import Settings
from animebot.core.registry import CommandRegistry
from animebot.domain.files import ChannelFile, PostFiles
from animebot.features.manga import handlers as manga_handlers
from animebot.features.manga.presenter import (
    author,
    format_hit_line,
    human_size,
    landing_keyboard,
    volumes,
    years,
)
from animebot.features.search import handlers as search_handlers
from animebot.search.callbacks import ANIME_PAGES, MANGA_PAGES, QueryStore
from animebot.search.service import SearchHit, SearchService
from animebot.storage.repo import PostRepo

from .conftest import MANGA_CHANNEL_ID, make_post

USER_ID = 7003
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)


def manga_post(message_id: int, title: str, **kw: Any):
    return make_post(message_id, title, channel_id=MANGA_CHANNEL_ID, **kw)


class Sent:
    def __init__(self) -> None:
        self.replies: list[tuple[str, InlineKeyboardMarkup | None]] = []
        self.edits: list[tuple[str, InlineKeyboardMarkup | None]] = []
        self.answers: list[str] = []
        self.alerts: list[str] = []


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    rec = Sent()

    async def fake_reply(self: Message, text: str, **kw: Any) -> None:
        rec.replies.append((text, kw.get("reply_markup")))

    async def fake_edit(self: Message, text: str, **kw: Any) -> None:
        rec.edits.append((text, kw.get("reply_markup")))

    async def fake_answer(self: CallbackQuery, text: str | None = None, **kw: Any) -> None:
        (rec.alerts if kw.get("show_alert") else rec.answers).append(text or "")

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    monkeypatch.setattr(Message, "edit_text", fake_edit, raising=True)
    monkeypatch.setattr(CallbackQuery, "answer", fake_answer, raising=True)
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
def dp(settings: Settings, repo: PostRepo, store: QueryStore) -> Dispatcher:
    """两个搜索模块都挂上 —— 测的就是它们的回调互不串台。"""
    d = Dispatcher()
    d["settings"] = settings
    d["repo"] = repo
    d["search"] = SearchService(repo, settings)
    d["manga_search"] = SearchService(repo, settings, channel=settings.manga)
    d["query_store"] = store
    reg = CommandRegistry()
    d["registry"] = reg

    search_router = Router(name="search")
    search_handlers.register_callbacks(search_router)
    manga_router = Router(name="manga")
    bind_commands(manga_router, collect_specs(manga_handlers), reg, "manga")
    manga_handlers.register_callbacks(manga_router)
    d.include_router(search_router)
    d.include_router(manga_router)
    return d


@pytest.fixture
async def library(repo: PostRepo) -> PostRepo:
    await repo.upsert_many([
        manga_post(99, "进击的巨人", tags=["热血", "漫画"], staff={"original": "諫山創"},
                   extra={"volumes": "34+外传"}, air_date="2009-09-09",
                   links={"bangumi": "https://bgm.tv/subject/1"}),
        *[manga_post(200 + i, f"测试漫画{i:02d}", tags=["漫画"]) for i in range(20)],
        make_post(1, "进击的巨人 动漫版"),
    ])
    await repo.upsert_files([
        ChannelFile(MANGA_CHANNEL_ID, 100, "Vol.01.zip", 1 << 30),
        ChannelFile(MANGA_CHANNEL_ID, 101, "Vol.02.zip", 1 << 30),
    ])
    return repo


def msg_update(text: str, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=NOW,
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text=text,
    ))


def cb_update(data: str, update_id: int = 1) -> Update:
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=f"cb{update_id}",
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci",
        data=data,
        message=Message(message_id=500, date=NOW, chat=Chat(id=USER_ID, type="private"),
                        text="旧消息"),
    ))


# ---------------------------------------------------------------- /c 指令


async def test_c_finds_manga_with_files(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, msg_update("/c 进击"))
    assert len(sent.replies) == 1
    text, markup = sent.replies[0]
    assert "https://t.me/c/1659887654/99" in text, "标题必须是跳漫画频道原帖的链接"
    assert "📦2个文件 2.0GB" in text
    assert "https://t.me/c/1659887654/100" in text, "文件链接跳第一个文件"
    assert "諫山創 · 34卷 · 2009" in text
    assert "Bangumi" in text
    assert "动漫版" not in text, "漫画搜索里混进了动漫帖"
    assert markup is None, "一页装得下就不该有翻页行"


async def test_c_aliases_route_to_same_handler(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, msg_update("/manga 进击", 1))
    await dp.feed_update(fake_bot, msg_update("/comic 进击", 2))
    assert len(sent.replies) == 2
    assert all("进击</b>的巨人" in t for t, _ in sent.replies)


async def test_c_typo_marked_as_near_match(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    """宽松模式纠错出来的结果，列表头上要说清楚是「名字相近」，不冒充命中。"""
    await dp.feed_update(fake_bot, msg_update("/c 进击巨人"))
    text, _ = sent.replies[0]
    assert "进击的巨人" in text
    assert "名字相近" in text


async def test_c_exact_hit_has_no_near_match_note(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, msg_update("/c 进击的巨人"))
    assert "名字相近" not in sent.replies[0][0]


async def test_c_nothing_found(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, msg_update("/c 完全不存在的作品名"))
    assert "没找到" in sent.replies[0][0]


async def test_c_without_query_shows_landing_with_tag_buttons(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, msg_update("/c"))
    text, markup = sent.replies[0]
    assert "共 21 部" in text
    assert markup is not None
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == ["c:0:l:#热血"], "#漫画 覆盖了全部帖子，什么也筛不掉，不该当按钮"


# ---------------------------------------------------------------- 回调


async def test_tag_button_edits_into_results(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, cb_update("c:0:l:#热血"))
    assert len(sent.edits) == 1
    assert "进击的巨人" in sent.edits[0][0]
    assert sent.answers == [""], "回调没 answer，客户端会一直转圈"


async def test_paging_uses_manga_prefix(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent, store: QueryStore
) -> None:
    await dp.feed_update(fake_bot, msg_update("/c 测试漫画"))
    _, markup = sent.replies[0]
    assert markup is not None
    nav = [b.callback_data for b in markup.inline_keyboard[-1]]
    assert nav[3] == "c:1:l:测试漫画"
    assert all(d == "cx" for d in nav[:3]), "首页的边界和页码按钮是漫画自己的占位"

    await dp.feed_update(fake_bot, cb_update("c:1:l:测试漫画", 2))
    assert "第 2/3 页" in sent.edits[0][0]


async def test_long_query_goes_through_token(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent, store: QueryStore
) -> None:
    data = MANGA_PAGES.encode(0, "测试漫画 " + "超长的查询词" * 6, store, strict=False)
    assert data.startswith("ct:")
    await dp.feed_update(fake_bot, cb_update(data))
    assert len(sent.edits) == 1


async def test_expired_token_alerts(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    await dp.feed_update(fake_bot, cb_update("ct:1:l:deadbeef"))
    assert sent.edits == []
    assert "过期" in sent.alerts[0]


async def test_noop_answered(dp: Dispatcher, fake_bot: Bot, sent: Sent) -> None:
    await dp.feed_update(fake_bot, cb_update("cx"))
    assert sent.answers == [""]
    assert sent.edits == []


async def test_anime_buttons_stay_with_anime(
    dp: Dispatcher, fake_bot: Bot, library: PostRepo, sent: Sent
) -> None:
    """聊天记录里的动漫翻页按钮（s:）必须还回到动漫结果，不能被漫画模块接走。"""
    await dp.feed_update(fake_bot, cb_update(ANIME_PAGES.encode(0, "进击", QueryStore(),
                                                                 strict=False)))
    text = sent.edits[0][0]
    assert "动漫版" in text
    assert "📚" not in text, "动漫按钮渲染成了漫画列表"


def test_prefixes_do_not_overlap() -> None:
    store = QueryStore()
    anime = ANIME_PAGES.encode(0, "q", store, strict=False)
    manga = MANGA_PAGES.encode(0, "q", store, strict=False)
    assert ANIME_PAGES.owns(anime) and not ANIME_PAGES.owns(manga)
    assert MANGA_PAGES.owns(manga) and not MANGA_PAGES.owns(anime)
    assert MANGA_PAGES.decode(anime, store) is None
    assert ANIME_PAGES.decode(manga, store) is None
    assert ANIME_PAGES.noop != MANGA_PAGES.noop
    assert not ANIME_PAGES.owns(None)


def test_legacy_anime_format_unchanged() -> None:
    """已经发出去的动漫翻页按钮长这样，部署后还得能点。"""
    ref = ANIME_PAGES.decode("s:3:l:青春", QueryStore())
    assert ref is not None
    assert (ref.page, ref.query, ref.strict) == (3, "青春", False)


# ---------------------------------------------------------------- 展示层


@pytest.mark.parametrize(
    ("vol", "eps", "want"),
    [
        ("20", "", "20卷"),
        ("14卷既刊 (2021-05-31)", "", "14卷"),
        ("34+外传+LOST GIRLS+Before the fall", "139", "34卷"),
        ("", "77+20EX(番外篇)", "77话"),
        ("全一卷", "", ""),
        ("", "", ""),
    ],
)
def test_volumes(vol: str, eps: str, want: str) -> None:
    extra = {"volumes": vol} if vol else {}
    assert volumes(manga_post(1, "X", extra=extra, episodes=eps)) == want


@pytest.mark.parametrize(
    ("air", "end", "release", "want"),
    [
        ("2009-09-09 (2009年10月号)", "2021-04-09", "", "2009–2021"),
        ("2017-06-24", "2017-12-01", "", "2017"),
        ("2017-06-24(2017年8月号)", "", "", "2017"),
        ("", "", "2011-10-21", "2011"),
        ("", "", "", ""),
    ],
)
def test_years(air: str, end: str, release: str, want: str) -> None:
    extra = {k: v for k, v in (("end_date", end), ("release_date", release)) if v}
    assert years(manga_post(1, "X", air_date=air, extra=extra)) == want


def test_author_prefers_original_then_art_and_clips() -> None:
    assert author(manga_post(1, "X", staff={"original": "A", "art": "B"})) == "A"
    assert author(manga_post(1, "X", staff={"art": "B"})) == "B"
    long = author(manga_post(1, "X", staff={"art": "信濃川日出雄、奥西チエ、吉田史朗、魚乃目三太"}))
    assert len(long) == 16
    assert long.endswith("…")


@pytest.mark.parametrize(
    ("n", "want"),
    [(11619577345, "10.8GB"), (437276660, "417MB"), (2048, "2KB")],
)
def test_human_size(n: int, want: str) -> None:
    assert human_size(n) == want


def test_bare_post_is_one_line() -> None:
    line = format_hit_line(1, SearchHit(manga_post(5, "无名"), 1.0, 1, "title"), [], None, None)
    assert "\n" not in line


def test_hit_line_escapes_and_links_files() -> None:
    post = manga_post(5, "A<B>", links={"online": "https://t.me/read/1"})
    line = format_hit_line(3, SearchHit(post, 1.0, 1, "title"), [], PostFiles(2, 2048, 6), None)
    assert "A&lt;B&gt;" in line
    assert 'href="https://t.me/c/1659887654/6">📦2个文件 2KB' in line
    assert ">在线看</a>" in line


def test_landing_keyboard_skips_tags_covering_most_posts() -> None:
    kb = landing_keyboard([("漫画", 662), ("恋爱", 119), ("BL", 80)], 938, QueryStore())
    assert kb is not None
    assert [b.text for row in kb.inline_keyboard for b in row] == ["#恋爱 119", "#BL 80"]


# ---------------------------------------------------------------- 整套装配


async def test_build_app_wires_manga_end_to_end(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, sent: Sent
) -> None:
    """真的 build_app：模块 setup 时放进容器的 manga_search 必须能注入到 handler。

    以前 Dispatcher 只抄了几个写死的键，模块自己 put 的东西 handler 拿不到 ——
    adding_a_feature.md 里那句「声明同名参数就能拿到」当时其实不成立。
    """
    from animebot.bot.app import build_app

    monkeypatch.setattr("animebot.bot.app.setup_logging", lambda **_kw: None)
    cfg = settings.model_copy(update={"bot_token": SecretStr("42:TEST-TOKEN-NOT-REAL")})
    app = await build_app(cfg)
    try:
        assert app.dp is not None and app.bot is not None
        assert isinstance(app.dp["manga_search"], SearchService)
        assert app.dp["manga_search"].channel.key == "manga"
        spec = app.registry.get("c")
        assert spec is not None and spec.feature == "manga"
        assert app.registry.get("comic") is spec

        await app.container.get("repo").upsert_many([manga_post(99, "进击的巨人")])
        await app.dp.feed_update(app.bot, msg_update("/c 进击"))
        assert sent.replies, "整条中间件链 + 注入走下来没有回复"
        assert "进击" in sent.replies[0][0]
    finally:
        await app.aclose()
