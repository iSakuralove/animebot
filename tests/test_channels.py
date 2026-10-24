"""多频道：动漫和漫画同库、同解析器、同步共用一个实例，靠 channel_id 隔开。

这一层最怕的不是报错，是「串台」：动漫搜索里混进漫画帖、漫画的标签云里出现
#漫改、文件挂到了别的频道的帖子名下。这些都不抛异常，只会让结果悄悄变脏，
所以每一条都要有断言盯着。
"""

from __future__ import annotations

import datetime as dt

import pytest
from aiogram.types import Animation, Chat, Document, Message, PhotoSize
from pydantic import ValidationError

from animebot.config import Settings
from animebot.domain.files import ChannelFile
from animebot.domain.post import ParseStatus
from animebot.ingest.export_loader import ExportMismatch, ingest_doc, resolve_channel
from animebot.ingest.sync import ChannelSync
from animebot.search.service import SearchService
from animebot.storage.repo import PostRepo

from .conftest import CHANNEL_ID, MANGA_CHANNEL_ID, make_post

MANGA_CHAT = int(f"-100{MANGA_CHANNEL_ID}")
ANIME_CHAT = int(f"-100{CHANNEL_ID}")
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)


def manga_post(message_id: int, title: str, **kw: object):
    return make_post(message_id, title, channel_id=MANGA_CHANNEL_ID, **kw)


def manga_file(message_id: int, size: int = 100) -> ChannelFile:
    return ChannelFile(MANGA_CHANNEL_ID, message_id, f"Vol.{message_id}.zip", size)


# ---------------------------------------------------------------- 配置


def test_channels_are_data(settings: Settings) -> None:
    assert [c.key for c in settings.channels] == ["anime", "manga"]
    assert settings.manga.channel_id == MANGA_CHANNEL_ID
    assert settings.manga.bot_api_chat_id == MANGA_CHAT
    assert settings.channel_by_id(MANGA_CHANNEL_ID) == settings.manga
    assert settings.channel_by_id(1) is None


def test_old_anime_properties_unchanged(settings: Settings) -> None:
    """Never break userspace：老代码读的这两个属性语义不能变。"""
    assert settings.bot_api_chat_id == ANIME_CHAT
    assert settings.link_username == "YXHMd"


def test_manga_id_accepts_bot_api_form(tmp_path) -> None:
    s = Settings(manga_channel_id=-1001659887654, db_path=tmp_path / "x.db")
    assert s.manga_channel_id == MANGA_CHANNEL_ID


def test_same_id_for_both_channels_rejected(tmp_path) -> None:
    with pytest.raises(ValidationError):
        Settings(channel_id=MANGA_CHANNEL_ID, manga_channel_id=MANGA_CHANNEL_ID,
                 db_path=tmp_path / "x.db")


def test_manga_links_fall_back_to_internal_without_username(settings: Settings) -> None:
    """导出里没有漫画频道的 username；没配时 public 模式也只能走 /c/ 链接。"""
    assert settings.manga.username == ""
    assert settings.manga.link_username("public") is None
    assert settings.anime.link_username("internal") is None


# ---------------------------------------------------------------- 检索作用域


@pytest.fixture
async def both(repo: PostRepo) -> PostRepo:
    """两个频道各有一部同名作品 + 同名标签，任何串台都会被看见。"""
    await repo.upsert_many([
        make_post(1, "进击的巨人", tags=["热血", "漫改"]),
        make_post(2, "咒术回战", tags=["热血"]),
        manga_post(99, "进击的巨人", tags=["热血", "漫画"]),
        manga_post(100, "排球少年", tags=["运动", "漫画"]),
    ])
    return repo


async def test_title_search_is_scoped(both: PostRepo, settings: Settings) -> None:
    anime = SearchService(both, settings)
    manga = SearchService(both, settings, channel=settings.manga)
    assert {h.post.channel_id for h in await anime.search("进击")} == {CHANNEL_ID}
    assert {h.post.channel_id for h in await manga.search("进击")} == {MANGA_CHANNEL_ID}


async def test_tag_search_is_scoped(both: PostRepo, settings: Settings) -> None:
    manga = SearchService(both, settings, channel=settings.manga)
    hits = await manga.search("#热血")
    assert [(h.post.channel_id, h.post.message_id) for h in hits] == [(MANGA_CHANNEL_ID, 99)]


async def test_fuzzy_is_scoped(both: PostRepo, settings: Settings) -> None:
    """fuzzy 读的是全量标题，最容易忘记带频道。"""
    anime = SearchService(both, settings)
    hits = await anime.search("排球少女")
    assert all(h.post.channel_id == CHANNEL_ID for h in hits), "动漫纠错纠出了漫画帖"


async def test_tag_cloud_and_counts_are_scoped(both: PostRepo) -> None:
    assert dict(await both.tag_cloud(channel_id=CHANNEL_ID)) == {"热血": 2, "漫改": 1}
    assert dict(await both.tag_cloud(channel_id=MANGA_CHANNEL_ID)) == {
        "漫画": 2, "热血": 1, "运动": 1,
    }
    assert await both.count(channel_id=CHANNEL_ID) == 2
    assert await both.count(channel_id=MANGA_CHANNEL_ID) == 2
    assert await both.count() == 4, "不带频道 = 全库合计（只给统计用）"
    assert await both.status_breakdown(channel_id=MANGA_CHANNEL_ID) == {"ok": 2}


# ---------------------------------------------------------------- 文件归属


async def test_files_belong_to_nearest_preceding_post(repo: PostRepo) -> None:
    """规则：文件属于 message_id 不大于它的、最近的那个有效帖子。

        5   文件      ← 前面没有帖子：孤儿，不归任何人
        10  帖子 A
        11  文件 ┐
        12  文件 │ A 的
        13  FAILED 帖（入库了但不算有效帖）
        14  文件 ┘ 仍然是 A 的
        20  帖子 B（自己就是带说明文字的文件）
        21  文件    B 的
    """
    await repo.upsert_many([
        manga_post(10, "A"),
        manga_post(13, "", parse_status=ParseStatus.FAILED),
        manga_post(20, "B"),
    ])
    await repo.upsert_files([
        manga_file(5), manga_file(11, 1000), manga_file(12, 2000), manga_file(14, 3000),
        manga_file(20, 7), manga_file(21, 8),
    ])
    stats = await repo.file_stats(MANGA_CHANNEL_ID, [10, 20])
    assert stats[10].count == 3
    assert stats[10].total_size == 6000
    assert stats[10].first_message_id == 11
    assert (stats[20].count, stats[20].total_size, stats[20].first_message_id) == (2, 15, 20)


async def test_posts_without_files_are_absent(repo: PostRepo) -> None:
    await repo.upsert_many([manga_post(10, "A"), manga_post(11, "B")])
    await repo.upsert_files([manga_file(12)])
    assert set(await repo.file_stats(MANGA_CHANNEL_ID, [10, 11])) == {11}
    assert await repo.file_stats(MANGA_CHANNEL_ID, []) == {}


async def test_files_do_not_cross_channels(repo: PostRepo) -> None:
    """动漫频道 #10 之后的文件，不能因为 message_id 落在区间里就算到漫画 #10 名下。"""
    await repo.upsert_many([make_post(10, "动漫帖"), manga_post(10, "漫画帖")])
    await repo.upsert_files([ChannelFile(CHANNEL_ID, 11, "anime.zip", 5)])
    assert await repo.file_stats(MANGA_CHANNEL_ID, [10]) == {}
    assert (await repo.file_stats(CHANNEL_ID, [10]))[10].count == 1


async def test_file_upsert_is_idempotent(repo: PostRepo) -> None:
    await repo.upsert_files([manga_file(11, 1)])
    await repo.upsert_files([manga_file(11, 2)])
    assert await repo.file_count(channel_id=MANGA_CHANNEL_ID) == 1


# ---------------------------------------------------------------- 回填路由


def _doc(channel_id: int, messages: list[dict]) -> dict:
    return {"name": "测试频道", "type": "public_channel", "id": channel_id, "messages": messages}


def test_resolve_channel(settings: Settings) -> None:
    assert resolve_channel(_doc(MANGA_CHANNEL_ID, []), settings.channels).key == "manga"
    assert resolve_channel(_doc(CHANNEL_ID, []), settings.channels).key == "anime"
    with pytest.raises(ExportMismatch, match="不是已配置的频道"):
        resolve_channel(_doc(1213081688, []), settings.channels)


async def test_ingest_doc_stores_posts_and_files(repo: PostRepo) -> None:
    ts = str(int(NOW.timestamp()))
    doc = _doc(MANGA_CHANNEL_ID, [
        {"id": 52, "type": "message", "date_unixtime": ts,
         "text": "中文名: 天降之物\n册数: 20\n标签：#漫画 #后宫", "photo": "x"},
        {"id": 54, "type": "message", "date_unixtime": ts, "text": "",
         "file": "x", "file_name": "[天降之物][20完].zip", "file_size": 2212425780,
         "mime_type": "application/zip"},
        {"id": 55, "type": "message", "date_unixtime": ts, "text": "",
         "file": "x", "media_type": "sticker", "file_name": "s.webp"},
    ])
    stats = await ingest_doc(doc, repo, source="test", expected_channel_id=MANGA_CHANNEL_ID)
    assert (stats.upserted, stats.files) == (1, 1)
    assert (await repo.file_stats(MANGA_CHANNEL_ID, [52]))[52].count == 1
    assert await repo.count(channel_id=CHANNEL_ID) == 0, "漫画导出写进了动漫频道"


# ---------------------------------------------------------------- 增量同步


@pytest.fixture
def sync(repo: PostRepo, settings: Settings) -> ChannelSync:
    return ChannelSync(repo, settings)


def channel_msg(chat_id: int, message_id: int, *, caption: str | None = None,
                document: Document | None = None, animation: Animation | None = None,
                photo: bool = False) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=chat_id, type="channel"),
        caption=caption,
        document=document,
        animation=animation,
        photo=[PhotoSize(file_id="p", file_unique_id="q", width=1, height=1)] if photo else None,
    )


ZIP = Document(file_id="f", file_unique_id="u", file_name="Vol.01.zip",
               file_size=437276660, mime_type="application/zip")


async def test_manga_post_synced_under_manga_channel(sync: ChannelSync, repo: PostRepo) -> None:
    msg = channel_msg(MANGA_CHAT, 1905, caption="中文名: 新连载\n册数: 1\n标签: #漫画", photo=True)
    result = await sync.handle(msg)
    assert result.stored
    assert await repo.get(MANGA_CHANNEL_ID, 1905) is not None
    assert await repo.get(CHANNEL_ID, 1905) is None


async def test_manga_document_attached(sync: ChannelSync, repo: PostRepo) -> None:
    await sync.handle(channel_msg(MANGA_CHAT, 1905, caption="中文名: 新连载\n册数: 1", photo=True))
    result = await sync.handle(channel_msg(MANGA_CHAT, 1906, document=ZIP))
    assert (result.action, result.reason) == ("attached", "file")
    assert not result.stored, "文件不是帖子，last_stored 水位不该动"
    files = await repo.file_stats(MANGA_CHANNEL_ID, [1905])
    assert files[1905].count == 1
    assert files[1905].total_size == 437276660
    state = await repo.sync_state(MANGA_CHANNEL_ID)
    assert state is not None
    assert state["last_seen_message_id"] == 1906
    assert state["last_stored_message_id"] == 1905


async def test_gif_is_not_a_file(sync: ChannelSync, repo: PostRepo) -> None:
    """Bot API 给 GIF 同时填 animation 和 document（向后兼容）—— 它不是资源文件。"""
    gif = Animation(file_id="a", file_unique_id="b", width=1, height=1, duration=1)
    doc = Document(file_id="a", file_unique_id="b", file_name="x.mp4", mime_type="video/mp4")
    result = await sync.handle(channel_msg(MANGA_CHAT, 1907, document=doc, animation=gif))
    assert result.reason == "empty"
    assert await repo.file_count() == 0


async def test_anime_sync_unchanged(sync: ChannelSync, repo: PostRepo) -> None:
    result = await sync.handle(channel_msg(ANIME_CHAT, 4000, caption="中文名: 番\n话数: 12"))
    assert result.stored
    assert await repo.get(CHANNEL_ID, 4000) is not None


async def test_unknown_chat_still_rejected(sync: ChannelSync, repo: PostRepo) -> None:
    result = await sync.handle(channel_msg(-1001213081688, 1, caption="中文名: 副本\n话数: 1"))
    assert result.reason == "wrong_chat"
    assert await repo.count(only_posts=False) == 0


async def test_watermarks_are_per_channel(sync: ChannelSync, settings: Settings) -> None:
    await sync.handle(channel_msg(ANIME_CHAT, 4000, caption="中文名: 番\n话数: 12"))
    await sync.handle(channel_msg(MANGA_CHAT, 1905, caption="中文名: 漫\n册数: 1"))
    anime = await sync.gap_report()
    manga = await sync.gap_report(channel=settings.manga)
    assert (anime["last_seen_message_id"], manga["last_seen_message_id"]) == (4000, 1905)
