"""增量同步的测试。

核心是 test_roundtrip_matches_export：拿真实导出消息反向构造成 aiogram
Message，走增量路径，断言得到的 Post 与回填路径完全相同。这是防两条入口
漂移的唯一手段 —— 没有它，adapter 写错了只会表现为"新帖的链接少了几个"，
而那要几个月后才有人发现。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from aiogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
)

from animebot.config import Settings
from animebot.domain.post import ParseStatus
from animebot.ingest.sync import ChannelSync
from animebot.ingest.update_adapter import (
    _utf16_len,
    message_to_export_shape,
)
from animebot.parsing.post_parser import parse_message
from animebot.parsing.text import flatten
from animebot.storage.repo import PostRepo

from .conftest import CHANNEL_ID, EXPORT_PATH

BOT_API_CHAT_ID = int(f"-100{CHANNEL_ID}")
NOW = dt.datetime(2026, 9, 5, 8, 54, 31, tzinfo=dt.UTC)
PHOTO = [PhotoSize(file_id="f", file_unique_id="u", width=500, height=707)]


def make_channel_message(
    text: str,
    *,
    message_id: int = 3948,
    entities: list[MessageEntity] | None = None,
    chat_id: int = BOT_API_CHAT_ID,
    photo: bool = True,
    buttons: list[tuple[str, str]] | None = None,
    edit_date: dt.datetime | None = None,
) -> Message:
    """造一条频道帖 update。

    `edit_date` 参数收 datetime 是为了测试可读，但塞进 Message 时要转成 int
    —— aiogram 的 `edit_date` 字段类型是 `int | None`，而 `date` 是 datetime。
    这个不一致就是 adapter 里 `_epoch()` 要同时接两种类型的原因。
    """
    markup = None
    if buttons:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t, url=u) for t, u in buttons]
            ]
        )
    return Message(
        message_id=message_id,
        date=NOW,
        edit_date=int(edit_date.timestamp()) if edit_date else None,
        chat=Chat(id=chat_id, type="channel", title="频道"),
        text=text,
        entities=entities,
        photo=PHOTO if photo else None,
        reply_markup=markup,
    )


# ---------------------------------------------------------------- UTF-16 偏移


def test_utf16_len_counts_code_units() -> None:
    assert _utf16_len("abc") == 3
    assert _utf16_len("中文") == 2
    assert _utf16_len("💙") == 2       # BMP 外，占两个码元
    assert _utf16_len("💙故事简介") == 6


def test_entity_offset_survives_emoji() -> None:
    """emoji 之后的链接必须落在正确的行上。

    Bot API 的 offset 按 UTF-16 码元计，而 Python 按码位。帖子正文里
    `💙故事简介`、`🔐解压`、`😱百度网盘` 到处都是，算错一次后面全错位。
    """
    text = "💙标题\n😱百度网盘：点击下载\n🔐解压:pw"
    # "点击下载" 在 UTF-16 里的偏移：💙(2)+标题(2)+\n(1)+😱(2)+百度网盘(4)+：(1) = 12
    off = _utf16_len("💙标题\n😱百度网盘：")
    ent = MessageEntity(type="text_link", offset=off, length=4,
                        url="https://pan.baidu.com/s/live")
    assert ent.extract_from(text) == "点击下载"

    shape = message_to_export_shape(make_channel_message(text, entities=[ent]))
    full, links = flatten(shape)
    assert full == text, "拍平后的文本必须与原文逐字相同"
    assert len(links) == 1
    assert links[0].url == "https://pan.baidu.com/s/live"
    assert links[0].line == 1, "链接归属到了错误的行"

    post = parse_message(shape, CHANNEL_ID)
    assert post is not None
    assert post.links["baidu"] == "https://pan.baidu.com/s/live"


def test_gaps_between_entities_are_filled() -> None:
    """entity 之间的空隙必须补成 plain，否则拍平后文本缺字、行号全错。"""
    text = "A链接1B链接2C"
    ents = [
        MessageEntity(type="text_link", offset=1, length=3, url="https://a.example"),
        MessageEntity(type="text_link", offset=5, length=3, url="https://b.example"),
    ]
    shape = message_to_export_shape(make_channel_message(text, entities=ents))
    full, links = flatten(shape)
    assert full == text
    assert [x.url for x in links] == ["https://a.example", "https://b.example"]


def test_unordered_entities() -> None:
    """不依赖 Telegram 保证 entity 有序。"""
    text = "第一行\n第二行"
    ents = [
        MessageEntity(type="text_link", offset=4, length=3, url="https://second.example"),
        MessageEntity(type="text_link", offset=0, length=3, url="https://first.example"),
    ]
    shape = message_to_export_shape(make_channel_message(text, entities=ents))
    full, links = flatten(shape)
    assert full == text
    assert [x.line for x in links] == [0, 1]


def test_bare_url_entity() -> None:
    """裸链接的 type 是 url，文本本身就是 URL，没有 href 字段。"""
    text = "🖼️阿里网盘：https://www.alipan.com/s/abc"
    off = _utf16_len("🖼️阿里网盘：")
    ent = MessageEntity(type="url", offset=off,
                        length=_utf16_len("https://www.alipan.com/s/abc"))
    shape = message_to_export_shape(make_channel_message(text, entities=[ent]))
    post = parse_message(shape, CHANNEL_ID)
    assert post is not None
    assert post.links["aliyun"] == "https://www.alipan.com/s/abc"


def test_nested_entity_ignored() -> None:
    """bold 套在 text_link 里时不能重复输出文本。"""
    text = "点击下载"
    ents = [
        MessageEntity(type="text_link", offset=0, length=4, url="https://x.example"),
        MessageEntity(type="bold", offset=0, length=2),
    ]
    shape = message_to_export_shape(make_channel_message(text, entities=ents))
    full, _ = flatten(shape)
    assert full == text, f"文本被重复或截断: {full!r}"


# ---------------------------------------------------------------- 形状转换


def test_caption_used_when_no_text() -> None:
    """图片帖的正文在 caption 里，不在 text。"""
    msg = Message(
        message_id=1, date=NOW,
        chat=Chat(id=BOT_API_CHAT_ID, type="channel"),
        caption="中文名: 测试\n话数: 12",
        photo=PHOTO,
    )
    post = parse_message(message_to_export_shape(msg), CHANNEL_ID)
    assert post is not None
    assert post.title_cn == "测试"
    assert post.episodes == "12"


def test_time_is_utc_from_update() -> None:
    shape = message_to_export_shape(make_channel_message("中文名: X\n话数: 1"))
    assert shape["date_unixtime"] == int(NOW.timestamp())
    post = parse_message(shape, CHANNEL_ID)
    assert post is not None
    assert post.posted_at == NOW
    assert post.posted_at.utcoffset() == dt.timedelta(0)


def test_edit_date_maps_to_edited() -> None:
    edited = NOW + dt.timedelta(hours=3)
    shape = message_to_export_shape(
        make_channel_message("中文名: X\n话数: 1", edit_date=edited)
    )
    post = parse_message(shape, CHANNEL_ID)
    assert post is not None
    assert post.edited_at == edited


def test_buttons_converted() -> None:
    shape = message_to_export_shape(
        make_channel_message(
            "中文名: X\n话数: 1",
            buttons=[("百度链接", "https://pan.baidu.com/s/btn"),
                     ("OD节点", "https://od.catimage.work/")],
        )
    )
    post = parse_message(shape, CHANNEL_ID)
    assert post is not None
    assert post.links["baidu"] == "https://pan.baidu.com/s/btn"
    assert post.links["od_node"] == "https://od.catimage.work/"


def test_photo_flag() -> None:
    with_photo = parse_message(
        message_to_export_shape(make_channel_message("中文名: X\n话数: 1")), CHANNEL_ID
    )
    without = parse_message(
        message_to_export_shape(
            make_channel_message("中文名: X\n话数: 1", photo=False)
        ),
        CHANNEL_ID,
    )
    assert with_photo is not None and without is not None
    assert with_photo.has_photo
    assert not without.has_photo


def test_no_file_id_stored() -> None:
    """file_id 是 per-bot 凭证，换 bot 就失效，存了也没用。"""
    shape = message_to_export_shape(make_channel_message("中文名: X\n话数: 1"))
    assert shape["photo"] == "(live update)"
    assert "file_id" not in json.dumps(shape)


# ---------------------------------------------------------------- ChannelSync


@pytest.fixture
def sync(repo: PostRepo, settings: Settings) -> ChannelSync:
    return ChannelSync(repo, settings)


async def test_new_post_stored(sync: ChannelSync, repo: PostRepo) -> None:
    msg = make_channel_message(
        "中文名: 增量测试番\n话数: 12\n标签：#奇幻 #异世界\n引索：#Z", message_id=4000
    )
    result = await sync.handle(msg)
    assert result.stored
    assert result.reason == "ok"

    stored = await repo.get(CHANNEL_ID, 4000)
    assert stored is not None
    assert stored.title_cn == "增量测试番"
    assert stored.tags == ["奇幻", "异世界"]
    assert stored.index_tags == ["Z"]


async def test_edit_overwrites(sync: ChannelSync, repo: PostRepo) -> None:
    """编辑不是边缘情况：真实数据里 1486/1486 的帖子都被编辑过。"""
    base = "中文名: 原标题\n话数: 12\n🗂百度网盘：https://pan.baidu.com/s/old"
    await sync.handle(make_channel_message(base, message_id=4001))

    fixed = "中文名: 修正标题\n话数: 24\n🗂百度网盘：https://pan.baidu.com/s/new"
    result = await sync.handle(
        make_channel_message(fixed, message_id=4001, edit_date=NOW), kind="edited"
    )
    assert result.stored

    stored = await repo.get(CHANNEL_ID, 4001)
    assert stored is not None
    assert stored.title_cn == "修正标题"
    assert stored.episodes == "24"
    assert stored.links["baidu"].endswith("/new")
    assert await repo.count() == 1, "编辑产生了重复行而不是覆盖"


async def test_tags_shrink_on_edit(sync: ChannelSync, repo: PostRepo) -> None:
    """编辑后标签变少，旧标签必须消失 —— 只 INSERT OR IGNORE 会留下幽灵标签。"""
    await sync.handle(
        make_channel_message("中文名: X\n话数: 1\n标签：#A #B #C", message_id=4002)
    )
    await sync.handle(
        make_channel_message("中文名: X\n话数: 1\n标签：#A", message_id=4002),
        kind="edited",
    )
    stored = await repo.get(CHANNEL_ID, 4002)
    assert stored is not None
    assert stored.tags == ["A"]


async def test_wrong_chat_rejected(sync: ChannelSync, repo: PostRepo) -> None:
    """讨论群的转发副本链接已失效，灌进来会让搜索结果指向死链。"""
    msg = make_channel_message(
        "中文名: 讨论群副本\n话数: 12", message_id=4003, chat_id=-1001213081688
    )
    result = await sync.handle(msg)
    assert not result.stored
    assert result.reason == "wrong_chat"
    assert await repo.count() == 0


async def test_non_post_skipped(sync: ChannelSync, repo: PostRepo) -> None:
    result = await sync.handle(
        make_channel_message("大家好，本频道明天维护", message_id=4004, photo=False)
    )
    assert not result.stored
    assert result.reason == "not_post"
    assert await repo.count() == 0


async def test_empty_media_group_member_skipped(sync: ChannelSync) -> None:
    """媒体组的第 2~n 张图没有 caption，正文为空 —— 正常情况，不是错误。"""
    msg = Message(
        message_id=4005, date=NOW,
        chat=Chat(id=BOT_API_CHAT_ID, type="channel"),
        photo=PHOTO,
    )
    result = await sync.handle(msg)
    assert not result.stored
    assert result.reason == "empty"


async def test_searchable_right_after_sync(
    sync: ChannelSync, search_service_factory
) -> None:
    """同步完立刻能搜到 —— 没有需要重建的索引。"""
    await sync.handle(
        make_channel_message(
            "中文名: 孤独摇滚\n话数: 12\n标签：#音乐 #搞笑", message_id=4006
        )
    )
    hits = await search_service_factory.search("孤独摇滚")
    assert [h.post.message_id for h in hits] == [4006]


# ---------------------------------------------------------------- 水位


async def test_watermark_tracks_both_seen_and_stored(
    sync: ChannelSync, repo: PostRepo
) -> None:
    """不入库的公告也要记 last_seen。

    只记 last_stored 的话，「一个月没发新番」和「同步挂了」在数据上完全一样，
    而这两件事的处置方式截然不同。
    """
    await sync.handle(
        make_channel_message("中文名: 番剧\n话数: 12", message_id=4100)
    )
    await sync.handle(
        make_channel_message("频道明天维护公告", message_id=4101, photo=False)
    )

    state = await repo.sync_state(CHANNEL_ID)
    assert state is not None
    assert state["last_seen_message_id"] == 4101, "公告没被计入 last_seen"
    assert state["last_stored_message_id"] == 4100
    assert state["seen_count"] == 2
    assert state["stored_count"] == 1


async def test_watermark_never_regresses(sync: ChannelSync, repo: PostRepo) -> None:
    """编辑旧帖会带来一个较小的 message_id，水位不能倒退。"""
    await sync.handle(make_channel_message("中文名: 新\n话数: 1", message_id=4200))
    await sync.handle(
        make_channel_message("中文名: 旧\n话数: 1", message_id=3000, edit_date=NOW),
        kind="edited",
    )
    state = await repo.sync_state(CHANNEL_ID)
    assert state is not None
    assert state["last_seen_message_id"] == 4200
    assert state["last_stored_message_id"] == 4200


async def test_wrong_chat_does_not_move_watermark(
    sync: ChannelSync, repo: PostRepo
) -> None:
    await sync.handle(
        make_channel_message("中文名: X\n话数: 1", message_id=9999,
                             chat_id=-1001213081688)
    )
    assert await repo.sync_state(CHANNEL_ID) is None


async def test_gap_report(sync: ChannelSync) -> None:
    await sync.handle(make_channel_message("中文名: X\n话数: 1", message_id=4300))
    report = await sync.gap_report(live_max_id=4310)
    assert report["db_max_message_id"] == 4300
    assert report["last_seen_message_id"] == 4300
    assert report["gap"] == 10
    assert report["stored_count"] == 1


async def test_gap_report_without_live_id(sync: ChannelSync) -> None:
    """不给真实水位时不该编一个 gap 出来。"""
    await sync.handle(make_channel_message("中文名: X\n话数: 1", message_id=4400))
    report = await sync.gap_report()
    assert "gap" not in report
    assert report["db_max_message_id"] == 4400


# ---------------------------------------------------------------- 防漂移


@pytest.mark.skipif(not EXPORT_PATH.exists(), reason="真实导出文件不在本机")
async def test_roundtrip_matches_export() -> None:
    """真实导出消息 → aiogram Message → Post，必须与直接解析导出的结果相同。

    这个测试是两条入口不漂移的唯一保证。adapter 写错了，症状只是"新帖少了
    几个链接"，几个月都不会有人发现。
    """
    with EXPORT_PATH.open(encoding="utf-8") as f:
        doc = json.load(f)

    checked = 0
    for raw in doc["messages"]:
        if raw.get("type") != "message" or "中文名" not in json.dumps(
            raw.get("text_entities", []), ensure_ascii=False
        ):
            continue

        want = parse_message(raw, CHANNEL_ID)
        if want is None or want.parse_status is ParseStatus.SKIPPED:
            continue

        # 把导出形状反向构造成 aiogram Message
        full, _ = flatten(raw)
        ents: list[MessageEntity] = []
        cursor = 0
        for seg in raw.get("text_entities", []):
            seg_len = _utf16_len(seg.get("text", ""))
            if seg.get("href"):
                ents.append(MessageEntity(type="text_link", offset=cursor,
                                          length=seg_len, url=seg["href"]))
            elif seg.get("type") == "link":
                ents.append(MessageEntity(type="url", offset=cursor, length=seg_len))
            cursor += seg_len

        buttons = [
            (b["text"], b["data"])
            for row in raw.get("inline_bot_buttons", [])
            for b in row
            if b.get("data")
        ]
        msg = make_channel_message(
            full,
            message_id=raw["id"],
            entities=ents or None,
            photo="photo" in raw,
            buttons=buttons or None,
        )
        got = parse_message(message_to_export_shape(msg), CHANNEL_ID)
        assert got is not None, f"id={raw['id']} 增量路径解析成 None"

        for field in ("title_cn", "title_en", "episodes", "air_date", "air_weekday",
                      "score", "score_text", "summary", "tags", "index_tags",
                      "passwords", "staff", "links", "raw_text", "parse_status"):
            assert getattr(got, field) == getattr(want, field), (
                f"id={raw['id']} 字段 {field} 两条入口不一致:\n"
                f"  回填: {getattr(want, field)!r}\n"
                f"  增量: {getattr(got, field)!r}"
            )
        checked += 1
        if checked >= 300:   # 300 条足够覆盖各年份的格式变体
            break

    assert checked >= 100, f"只对比了 {checked} 条，样本太少"
