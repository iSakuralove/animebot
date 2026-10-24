"""漫画频道的解析：新写法的单元测试 + 938 帖黄金数据集 + 两条入口防漂移。

漫画频道和动漫频道共用解析器和别名表（同一个 up 主、同一套模板），所以这里
守的是两件事：漫画帖的字段真的被读懂了；为漫画加的写法没有把动漫帖带坏
（后者由 test_parser_golden.py 守着，这里不重复）。

基线（2026-10-24 导出实测）：
    消息 1257  ok 896  partial 42  failed 0  帖子 938  文件 271
"""

from __future__ import annotations

import bisect
import collections
import datetime as dt
import json
from typing import Any

import pytest
from aiogram.types import Chat, Document, Message, MessageEntity, PhotoSize

from animebot.config import Settings
from animebot.domain.fields import META_ALIASES
from animebot.domain.links import DIRECT, INDEX, INFO, OFFICIAL, categorize, kind_of_url
from animebot.domain.post import ParseStatus
from animebot.ingest.export_loader import (
    ExportMismatch,
    parse_export,
    parse_export_files,
    resolve_channel,
)
from animebot.ingest.update_adapter import _utf16_len, message_to_export_shape
from animebot.parsing.files import parse_file
from animebot.parsing.post_parser import parse_message
from animebot.parsing.text import flatten

from .conftest import CHANNEL_ID, MANGA_CHANNEL_ID, MANGA_EXPORT_PATH

MANGA_CHAT = int(f"-100{MANGA_CHANNEL_ID}")
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)


def plain(s: str) -> dict[str, Any]:
    return {"type": "plain", "text": s}


def tlink(text: str, href: str) -> dict[str, Any]:
    return {"type": "text_link", "text": text, "href": href}


def link(url: str) -> dict[str, Any]:
    return {"type": "link", "text": url}


def export_msg(*segments: dict[str, Any], mid: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "type": "message",
        "date_unixtime": str(int(NOW.timestamp())),
        "text_entities": list(segments),
        **extra,
    }


def parse(*segments: dict[str, Any], **extra: Any):
    post = parse_message(export_msg(*segments, **extra), MANGA_CHANNEL_ID)
    assert post is not None
    return post


# ---------------------------------------------------------------- 新模板（2024 起）

NEW_TEMPLATE = (
    "中文名: 4分钟的金盏菊\n"
    "别名: marigold in 4 minutes、四分间的金盏花\n"
    "作者: キリエ\n"
    "出版社: 小学馆\n"
    "连载杂志: 週刊ビッグコミックスピリッツ\n"
    "发售日: 2017-08-10\n"
    "册数: 3\n"
    "话数: 27\n"
    "开始: 2017-05-15(2017年24号)\n"
    "结束: 2018-02-05(2018年10号)\n"
    "版本:尖端版: 4分钟的金盏菊、繁体中文、尖端出版\n"
    "版本:东立版: 另一个版本\n"
    "ISBN: 9784091892423\n"
    "ISBN-10: 4091892423\n"
    "标签: #漫画 #姐弟 #恋爱\n"
    "\n"
    "简介\n"
    "本作讲述能够看到人的死亡命运的急救医生。\n"
    "\n"
    "链接: "
)


class TestNewTemplate:
    @pytest.fixture
    def post(self):
        return parse(
            plain(NEW_TEMPLATE),
            tlink("Bangumi", "https://bgm.tv/subject/214937"),
            plain("\n\n资源请在评论区拿"),
            photo="(File not included.)",
        )

    def test_status_and_title(self, post) -> None:
        assert post.parse_status is ParseStatus.OK
        assert post.title_cn == "4分钟的金盏菊"
        assert post.aliases == ["marigold in 4 minutes", "四分间的金盏花"]

    def test_people(self, post) -> None:
        assert post.staff["original"] == "キリエ"
        assert post.staff["publisher"] == "小学馆"

    def test_book_fields_land_in_extra_under_canonical_names(self, post) -> None:
        assert post.extra["volumes"] == "3"
        assert post.extra["magazine"] == "週刊ビッグコミックスピリッツ"
        assert post.extra["release_date"] == "2017-08-10"
        assert post.extra["end_date"].startswith("2018-02-05")

    def test_first_occurrence_wins(self, post) -> None:
        """ISBN 先于 ISBN-10、第一行版本是主版本 —— 和标题、链接一个规矩。"""
        assert post.extra["isbn"] == "9784091892423"
        assert post.extra["edition"].startswith("尖端版")

    def test_serialization_start_is_air_date(self, post) -> None:
        """「开始」对漫画是连载开始，和番剧的放送开始一样落 air_date。"""
        assert post.air_date.startswith("2017-05-15")
        assert post.episodes == "27"

    def test_bangumi_link_resolved_by_host(self, post) -> None:
        assert post.links == {"bangumi": "https://bgm.tv/subject/214937"}
        assert post.info_links == [("bangumi", "Bangumi", "https://bgm.tv/subject/214937")]
        assert post.direct_links == [], "资料页不是下载链接"

    def test_summary_ends_at_link_line(self, post) -> None:
        """「链接」以前是噪音词，简介会一直吞到「资源请在评论区拿」。"""
        assert post.summary == "本作讲述能够看到人的死亡命运的急救医生。"


# ---------------------------------------------------------------- 别名续行


def test_alias_continuation_lines() -> None:
    post = parse(plain(
        "中文名: 蓝色时期\n别名: 藍色時期\n青色终止符\nBlue Period.\n发售日: 2017-12-22\n"
        "标签：#漫画 #艺术"
    ))
    assert post.aliases == ["藍色時期", "青色终止符", "Blue Period."]
    assert "Blue Period" in post.search_title


@pytest.mark.parametrize(
    ("after", "why"),
    [
        ("\n#漫画 #恋爱", "标签行不是别名"),
        ("\n\n下一段正文", "空行结束续行"),
        ("\n" + "很长的一段简介正文" * 8, "超过 60 字的是正文"),
        ("\n作者: 某人", "下一个字段结束续行"),
    ],
)
def test_alias_continuation_stops(after: str, why: str) -> None:
    post = parse(plain("中文名: X\n别名: 甲" + after + "\n话数: 1"))
    assert post.aliases == ["甲"], why


def test_title_from_alias_when_no_chinese_name() -> None:
    """漫画 #555：通篇没有中文名。旧解析器把第二行「发售日: …」当成了标题。"""
    post = parse(plain("别名: 排球!!\n发售日: 2012-06-04\n话数: 402\n标签：#漫画 #运动"))
    assert post.title_cn == "排球!!"
    assert "title_from_alias" in post.parse_notes
    assert post.parse_status is ParseStatus.OK


def test_story_is_a_summary_head() -> None:
    post = parse(plain(
        "神枪少女\n评分: 8.6\n标签: #漫画\n\nSTORY: \n故事舞台是在现代的意大利都市。"
    ))
    assert post.summary == "故事舞台是在现代的意大利都市。"
    assert "STORY" not in post.extra


# ---------------------------------------------------------------- 链接


def test_generic_link_line_resolves_by_host() -> None:
    """「链接：」不说指向哪里。动漫频道那几行指向百度，必须落成 baidu 才会出现在结果里。"""
    url = "https://pan.baidu.com/s/1cCusgK851NAiZBNXFx6l-A?pwd=2gm6"
    post = parse(plain("中文名: X\n话数: 12\n链接："), link(url))
    assert post.links == {"baidu": url}
    assert [k for k, _, _ in post.direct_links] == ["baidu"]


def test_link_header_then_named_lines() -> None:
    """老模板：「链接：」空着，下面一行一个网盘。"""
    post = parse(
        plain("中文名: X\n册数: 14\n链接：\n蓝奏："),
        link("https://yxhmbookshare.lanzouy.com/b02q3n6pg"),
        plain("\n标签：#轻小说"),
    )
    assert post.links == {"lanzou": "https://yxhmbookshare.lanzouy.com/b02q3n6pg"}


def test_online_reading_is_info_not_download() -> None:
    url = "https://t.me/letreadbookintelegraphnow/41"
    post = parse(plain("中文名: 天降之物\n册数: 20\n在线观看："), link(url))
    assert post.links == {"online": url}
    assert post.info_links == [("online", "在线看", url)]
    assert post.direct_links == []


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://bgm.tv/subject/1", "bangumi"),
        ("https://bangumi.tv/subject/1", "bangumi"),
        ("https://pan.baidu.com/s/x", "baidu"),
        ("https://disk.yandex.ru/d/x", "yandex"),
        ("https://yxhmbookshare.lanzouj.com/b0", "lanzou"),
        ("https://pan.quark.cn/s/x", "quark"),
        ("https://www.alipan.com/s/x", "aliyun"),
        ("https://manga.bilibili.com/detail/mc1", "web"),
    ],
)
def test_kind_of_url(url: str, kind: str) -> None:
    assert kind_of_url(url) == kind


def test_categorize_always_has_four_groups() -> None:
    groups = categorize({"baidu": "https://pan.baidu.com/s/x", "bangumi": "https://bgm.tv/s/1"})
    assert set(groups) == {DIRECT, INDEX, OFFICIAL, INFO}
    assert [k for k, _, _ in groups[DIRECT]] == ["baidu"]
    assert [k for k, _, _ in groups[INFO]] == ["bangumi"]


# ---------------------------------------------------------------- 文件消息


def test_document_is_a_file() -> None:
    raw = export_msg(
        mid=54,
        file="(File not included. Change data exporting settings to download.)",
        file_name="[天降之物][水無月すう][角川][20完].zip",
        file_size=2212425780,
        mime_type="application/zip",
    )
    f = parse_file(raw, MANGA_CHANNEL_ID)
    assert f is not None
    assert (f.channel_id, f.message_id) == (MANGA_CHANNEL_ID, 54)
    assert f.file_name.endswith(".zip")
    assert f.file_size == 2212425780
    assert parse_message(raw, MANGA_CHANNEL_ID) is None, "没有正文，不是帖子"


@pytest.mark.parametrize(
    "extra",
    [
        {"file": "x", "media_type": "sticker", "file_name": "s.webp"},
        {"file": "x", "media_type": "animation", "file_name": "a.mp4"},
        {"photo": "x"},
        {},
    ],
)
def test_non_documents_are_not_files(extra: dict[str, Any]) -> None:
    assert parse_file(export_msg(**extra), MANGA_CHANNEL_ID) is None


def test_service_message_is_not_a_file() -> None:
    assert parse_file({"id": 1, "type": "service", "file": "x"}, MANGA_CHANNEL_ID) is None


# ---------------------------------------------------------------- 黄金数据集

needs_export = pytest.mark.skipif(
    not MANGA_EXPORT_PATH.exists(), reason="漫画频道导出文件不在本机"
)


@pytest.fixture(scope="module")
def manga_doc() -> dict[str, Any]:
    with MANGA_EXPORT_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def manga_parsed(manga_doc):
    return parse_export(manga_doc, expected_channel_id=MANGA_CHANNEL_ID)


@needs_export
def test_golden_no_failures(manga_parsed) -> None:
    posts, stats = manga_parsed
    assert stats.failed == 0, f"出现 {stats.failed} 条解析失败"
    assert len(posts) >= 938, f"帖子数从 938 掉到 {len(posts)}"
    assert stats.ok >= 896, f"ok 从 896 掉到 {stats.ok}"


@needs_export
@pytest.mark.parametrize(
    ("field", "getter", "floor"),
    [
        ("title_cn", lambda p: bool(p.title_cn), 938),
        ("aliases", lambda p: bool(p.aliases), 753),
        ("tags", lambda p: bool(p.tags), 877),
        ("index_tags", lambda p: bool(p.index_tags), 153),
        ("summary", lambda p: bool(p.summary), 934),
        ("author", lambda p: bool(p.staff.get("original") or p.staff.get("art")), 791),
        ("air_date", lambda p: bool(p.air_date), 448),
        ("score", lambda p: p.score is not None, 258),
        ("episodes", lambda p: bool(p.episodes), 496),
        ("volumes", lambda p: bool(p.extra.get("volumes")), 505),
        ("release_date", lambda p: bool(p.extra.get("release_date")), 880),
        ("bangumi", lambda p: "bangumi" in p.links, 755),
    ],
)
def test_golden_field_coverage(manga_parsed, field, getter, floor) -> None:
    posts, _ = manga_parsed
    got = sum(1 for p in posts if getter(p))
    assert got >= floor, f"{field} 填充数从 {floor} 掉到 {got}"


@needs_export
def test_golden_no_unknown_frequent_keys(manga_parsed) -> None:
    """出现 >=5 次、又不是我们登记过的规范名，就是别名表漏了写法。"""
    posts, _ = manga_parsed
    counter = collections.Counter(k for p in posts for k in p.extra if k not in META_ALIASES)
    frequent = {k: n for k, n in counter.items() if n >= 5}
    assert not frequent, f"这些字段名还没登记进别名表: {frequent}"


@needs_export
def test_golden_known_posts(manga_parsed) -> None:
    by = {p.message_id: p for p in manga_parsed[0]}
    aot = by[99]
    assert aot.title_cn == "进击的巨人"
    assert aot.staff["original"] == "諫山創"
    assert aot.extra["volumes"].startswith("34")
    assert aot.air_date.startswith("2009-09-09")
    assert aot.extra["end_date"].startswith("2021-04-09")

    tennis = by[1900]
    assert tennis.links["bangumi"] == "https://bgm.tv/subject/40643"
    assert "manga.bilibili.com" in tennis.links["web"]

    assert "Blue Period." in by[55].aliases
    assert by[555].title_cn == "排球!!"


@needs_export
def test_golden_files_all_have_an_owner(manga_doc, manga_parsed) -> None:
    """文件 = 紧跟在资料帖后面的整卷压缩包。每一个都得找得到主人。"""
    files = parse_export_files(manga_doc)
    assert len(files) >= 271
    ids = sorted(p.message_id for p in manga_parsed[0])
    orphans = [f.message_id for f in files if bisect.bisect_right(ids, f.message_id) == 0]
    assert not orphans, f"这些文件前面没有帖子: {orphans}"


@needs_export
def test_golden_routed_to_manga_not_anime(manga_doc, settings: Settings) -> None:
    assert resolve_channel(manga_doc, settings.channels).key == "manga"
    with pytest.raises(ExportMismatch):
        parse_export(manga_doc, expected_channel_id=CHANNEL_ID)


# ---------------------------------------------------------------- 两条入口不漂移


def _to_aiogram(raw: dict[str, Any]) -> Message:
    """导出 JSON 的一条消息 -> 增量同步会收到的 aiogram Message。"""
    full, _ = flatten(raw)
    ents: list[MessageEntity] = []
    cursor = 0
    for seg in raw.get("text_entities", []):
        seg_len = _utf16_len(seg.get("text", ""))
        if seg.get("href"):
            ents.append(MessageEntity(type="text_link", offset=cursor, length=seg_len,
                                      url=seg["href"]))
        elif seg.get("type") == "link":
            ents.append(MessageEntity(type="url", offset=cursor, length=seg_len))
        cursor += seg_len
    doc = None
    if "file" in raw and not raw.get("media_type"):
        doc = Document(file_id="f", file_unique_id="u", file_name=raw.get("file_name"),
                       file_size=raw.get("file_size"), mime_type=raw.get("mime_type"))
    return Message(
        message_id=raw["id"],
        date=NOW,
        chat=Chat(id=MANGA_CHAT, type="channel"),
        caption=full or None,
        caption_entities=ents or None,
        photo=[PhotoSize(file_id="p", file_unique_id="q", width=1, height=1)]
        if "photo" in raw else None,
        document=doc,
    )


@needs_export
def test_roundtrip_posts_match_export(manga_doc) -> None:
    checked = 0
    for raw in manga_doc["messages"]:
        want = parse_message(raw, MANGA_CHANNEL_ID)
        if want is None or want.parse_status is ParseStatus.SKIPPED:
            continue
        got = parse_message(message_to_export_shape(_to_aiogram(raw)), MANGA_CHANNEL_ID)
        assert got is not None, f"id={raw['id']} 增量路径解析成 None"
        for field in ("title_cn", "aliases", "episodes", "air_date", "score", "summary",
                      "tags", "index_tags", "staff", "links", "extra", "raw_text",
                      "parse_status"):
            assert getattr(got, field) == getattr(want, field), (
                f"id={raw['id']} 字段 {field} 两条入口不一致:\n"
                f"  回填: {getattr(want, field)!r}\n  增量: {getattr(got, field)!r}"
            )
        checked += 1
        if checked >= 300:
            break
    assert checked >= 100, f"只对比了 {checked} 条，样本太少"


@needs_export
def test_roundtrip_files_match_export(manga_doc) -> None:
    raws = [m for m in manga_doc["messages"] if parse_file(m, MANGA_CHANNEL_ID)]
    assert len(raws) >= 271
    for raw in raws[:60]:
        want = parse_file(raw, MANGA_CHANNEL_ID)
        got = parse_file(message_to_export_shape(_to_aiogram(raw)), MANGA_CHANNEL_ID)
        assert got == want, f"id={raw['id']} 文件两条入口不一致: {want} vs {got}"
