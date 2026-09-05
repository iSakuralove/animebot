"""解析器的单元测试。用手写的最小样本盯住每条规则，不依赖真实导出。"""

from __future__ import annotations

from typing import Any

from animebot.domain.fields import normalize_key
from animebot.domain.post import ParseStatus
from animebot.parsing.post_parser import parse_message
from animebot.parsing.text import flatten, split_kv

CHANNEL = 1702674582


def msg(text: Any, **kw: Any) -> dict[str, Any]:
    """造一条导出格式的消息。text 传 str 或 entity 列表。"""
    ents = [{"type": "plain", "text": text}] if isinstance(text, str) else text
    return {
        "id": kw.pop("id", 1),
        "type": "message",
        "date": kw.pop("date", "2025-01-01T12:00:00"),
        "text": ents,
        "text_entities": ents,
        **kw,
    }


class TestNormalizeKey:
    def test_strips_emoji(self) -> None:
        assert normalize_key("☺️评分") == "评分"
        assert normalize_key("⭐️评分") == "评分"
        assert normalize_key("🩶GoogleDrive") == "GoogleDrive"
        assert normalize_key("⛓️解压") == "解压"
        assert normalize_key("😱百度网盘") == "百度网盘"

    def test_keeps_japanese(self) -> None:
        assert normalize_key("アニメーション制作") == "アニメーション制作"
        assert normalize_key("音楽") == "音楽"


class TestSplitKV:
    def test_both_colon_forms(self) -> None:
        assert split_kv("中文名: 少女与战车") == ("中文名", "少女与战车")
        assert split_kv("中文名：少女与战车") == ("中文名", "少女与战车")

    def test_no_colon(self) -> None:
        assert split_kv("故事简介") is None

    def test_long_key_rejected(self) -> None:
        """key 超过 14 字的行不是字段，是正文里带冒号的句子。"""
        assert split_kv("这是一段很长的正文内容超过十四个字了：所以不是字段") is None

    def test_substring_does_not_match_wrong_field(self) -> None:
        """'放送开始' 不能被 '开始' 匹配走 —— 老代码的 `in text` 就是这么错的。"""
        kv = split_kv("放送开始: 2025年10月1日")
        assert kv is not None
        assert kv[0] == "放送开始"
        assert normalize_key(kv[0]) != "开始"


class TestFlatten:
    def test_entity_list_becomes_text(self) -> None:
        text, links = flatten(msg([
            {"type": "bold", "text": "中文名: 测试\n"},
            {"type": "plain", "text": "话数: 12"},
        ]))
        assert text == "中文名: 测试\n话数: 12"
        assert links == []

    def test_hidden_link_line_attribution(self) -> None:
        """锚文本是 '\\n' 的隐藏链接，必须归到它所在的那一行。"""
        _, links = flatten(msg([
            {"type": "plain", "text": "百度网盘："},
            {"type": "text_link", "text": "点击下载\n", "href": "https://pan.baidu.com/s/a"},
            {"type": "plain", "text": "解压：abc"},
        ]))
        assert len(links) == 1
        assert links[0].line == 0
        assert links[0].url == "https://pan.baidu.com/s/a"

    def test_plain_string_text(self) -> None:
        text, _ = flatten({"id": 1, "type": "message", "text": "纯字符串"})
        assert text == "纯字符串"


class TestParseMessage:
    def test_service_message_ignored(self) -> None:
        assert parse_message({"id": 1, "type": "service", "text": ""}, CHANNEL) is None

    def test_full_template(self) -> None:
        post = parse_message(msg(
            "中文名: 无职英雄\n"
            "英文名: Mushoku no Eiyuu\n"
            "话数: 12\n"
            "放送开始: 2025年10月1日\n"
            "放送星期: 星期三\n"
            "导演: 矢花馨\n"
            "☺️评分：4.9 不过不失\n\n"
            "故事简介\n"
            "一个没有技能的故事\n\n"
            "🔐解压:blackcatunderthemoon\n\n"
            "引索：#W #WZ\n"
            "标签：#轻改 #奇幻\n",
            id=100,
        ), CHANNEL)
        assert post is not None
        assert post.parse_status is ParseStatus.OK
        assert post.title_cn == "无职英雄"
        assert post.title_en == "Mushoku no Eiyuu"
        assert post.episodes == "12"
        assert post.air_weekday == "星期三"
        assert post.score == 4.9
        assert post.score_text == "不过不失"
        assert post.summary == "一个没有技能的故事"
        assert post.index_tags == ["W", "WZ"]
        assert post.tags == ["轻改", "奇幻"]
        assert post.passwords == ["blackcatunderthemoon"]
        assert post.staff == {"director": "矢花馨"}
        assert post.key == (CHANNEL, 100)

    def test_title_with_colon_kept_whole(self) -> None:
        post = parse_message(msg("Re：从零开始的异世界生活\n\n概况介绍\n某个故事"), CHANNEL)
        assert post is not None
        assert post.title_cn == "Re：从零开始的异世界生活"
        assert "title_from_first_line" in post.parse_notes

    def test_summary_with_colon_inside(self) -> None:
        """简介正文里的冒号不能被当字段切走。"""
        post = parse_message(msg(
            "中文名: 测试\n话数: 12\n\n故事简介\n"
            "少年说：我要成为海贼王\n第二段也在简介里\n"
        ), CHANNEL)
        assert post is not None
        assert "我要成为海贼王" in post.summary
        assert "第二段也在简介里" in post.summary
        assert post.extra == {}

    def test_unknown_key_goes_to_extra(self) -> None:
        post = parse_message(msg("中文名: 测试\n话数: 12\n某个新字段: 某个值"), CHANNEL)
        assert post is not None
        assert post.extra["某个新字段"] == "某个值"

    def test_emoji_prefixed_score_variants(self) -> None:
        for prefix in ("☺️", "⭐️", "☕️", ""):
            post = parse_message(msg(f"中文名: 测试\n话数: 12\n{prefix}评分：7.6 力荐"), CHANNEL)
            assert post is not None, prefix
            assert post.score == 7.6, prefix
            assert post.score_text == "力荐", prefix

    def test_links_from_entities(self) -> None:
        post = parse_message(msg([
            {"type": "plain", "text": "中文名: 测试\n话数: 12\n😱百度网盘："},
            {"type": "text_link", "text": "点击下载\n", "href": "https://pan.baidu.com/s/a"},
            {"type": "plain", "text": "💔OneDrive："},
            {"type": "text_link", "text": "打开表格\n", "href": "https://x-my.sharepoint.com/y"},
        ]), CHANNEL)
        assert post is not None
        assert post.links["baidu"] == "https://pan.baidu.com/s/a"
        assert post.links["onedrive"] == "https://x-my.sharepoint.com/y"

    def test_shared_sheet_dropped(self) -> None:
        post = parse_message(msg([
            {"type": "plain", "text": "中文名: 测试\n话数: 12\n😄往期番剧汇总表格："},
            {
                "type": "text_link",
                "text": "打开\n",
                "href": "https://docs.google.com/spreadsheets/d/"
                        "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX/edit",
            },
        ]), CHANNEL)
        assert post is not None
        assert "sheet" not in post.links

    def test_inline_buttons_as_link_source(self) -> None:
        post = parse_message(msg(
            "中文名: 测试\n话数: 12",
            inline_bot_buttons=[[
                {"type": "url", "text": "百度链接", "data": "https://pan.baidu.com/s/b"},
                {"type": "url", "text": "OD节点", "data": "https://od.catimage.work/"},
            ]],
        ), CHANNEL)
        assert post is not None
        assert post.links["baidu"] == "https://pan.baidu.com/s/b"
        assert post.links["od_node"] == "https://od.catimage.work/"

    def test_backup_password_collected(self) -> None:
        post = parse_message(msg(
            "中文名: 测试\n话数: 12\n⛓解压：blackcat\n如密码不对该换 ：千秋漫雪"
        ), CHANNEL)
        assert post is not None
        assert post.passwords == ["blackcat", "千秋漫雪"]

    def test_announcement_is_skipped(self) -> None:
        post = parse_message(msg("大家好，本频道禁止水群，违者踢出"), CHANNEL)
        assert post is not None
        assert post.parse_status is ParseStatus.SKIPPED

    def test_raw_text_always_kept(self) -> None:
        raw = "中文名: 测试\n话数: 12"
        post = parse_message(msg(raw), CHANNEL)
        assert post is not None
        assert post.raw_text == raw
