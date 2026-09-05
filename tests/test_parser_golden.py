"""解析器的黄金回归测试。

核心思路：1639 个真实帖子就是黄金数据集。解析器任何改动都不许让指标退化
—— 阈值卡在当前实测值略下方，改坏了立刻红。

这些数字是 2026-09-05 从真实导出跑出来的基线：
    ok=1504  partial=135  failed=0  帖子=1639
"""

from __future__ import annotations

import collections
import json

import pytest

from animebot.domain.post import ParseStatus
from animebot.ingest.export_loader import ExportMismatch, parse_export
from animebot.parsing.post_parser import parse_message

from .conftest import CHANNEL_ID, EXPORT_PATH

pytestmark = pytest.mark.skipif(
    not EXPORT_PATH.exists(), reason="真实导出文件不在本机"
)


@pytest.fixture(scope="module")
def export_doc() -> dict:
    with EXPORT_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def parsed(export_doc: dict):
    posts, stats = parse_export(export_doc, expected_channel_id=CHANNEL_ID)
    return posts, stats


def test_no_parse_failures(parsed) -> None:
    """failed 必须永远是 0。有标题却认不出来 = 解析器漏了写法。"""
    _, stats = parsed
    assert stats.failed == 0, f"出现 {stats.failed} 条解析失败"


def test_post_count_no_regression(parsed) -> None:
    posts, _ = parsed
    assert len(posts) >= 1639, f"帖子数从 1639 掉到 {len(posts)}"


def test_ok_ratio_no_regression(parsed) -> None:
    _, stats = parsed
    assert stats.ok >= 1504, f"ok 从 1504 掉到 {stats.ok}"


@pytest.mark.parametrize(
    ("field", "getter", "floor"),
    [
        ("title_cn", lambda p: bool(p.title_cn), 1639),
        ("episodes", lambda p: bool(p.episodes), 1478),
        ("air_date", lambda p: bool(p.air_date), 1366),
        ("score", lambda p: p.score is not None, 1468),
        ("summary", lambda p: bool(p.summary), 1577),
        ("tags", lambda p: bool(p.tags), 1591),
        ("index_tags", lambda p: bool(p.index_tags), 1493),
        ("passwords", lambda p: bool(p.passwords), 1604),
        ("links", lambda p: bool(p.links), 1606),
        ("staff", lambda p: bool(p.staff), 1479),
    ],
)
def test_field_coverage_no_regression(parsed, field, getter, floor) -> None:
    """每个字段的填充数不许掉。加别名可以让它涨，不能让它跌。"""
    posts, _ = parsed
    got = sum(1 for p in posts if getter(p))
    assert got >= floor, f"{field} 填充数从 {floor} 掉到 {got}"


def test_no_unknown_frequent_keys(parsed) -> None:
    """出现 >=5 次的未知字段说明别名表漏了写法，应该补进 fields.py。"""
    posts, _ = parsed
    counter = collections.Counter(k for p in posts for k in p.extra)
    known_canonical = ("extract", "volume", "file")   # 我们故意放进 extra 的规范键
    frequent = {
        k: n
        for k, n in counter.items()
        if n >= 5 and not k.startswith(known_canonical)
    }
    assert not frequent, f"这些字段名还没登记进别名表: {frequent}"


def test_titles_with_colon_survive(parsed) -> None:
    """'Re：从零开始的异世界生活' 这类标题自带冒号，曾经被当成 key: value 丢掉。"""
    posts, _ = parsed
    titles = {p.title_cn for p in posts}
    assert any(t.startswith("Re") and "从零开始" in t for t in titles), \
        "带冒号的标题又被吞了"


def test_known_post_fully_parsed(export_doc) -> None:
    """拿一条最新的真实帖子逐字段核对。格式变了这里第一个红。"""
    msg = next(m for m in export_doc["messages"] if m.get("id") == 3948)
    post = parse_message(msg, CHANNEL_ID)
    assert post is not None
    assert post.parse_status is ParseStatus.OK
    assert post.title_cn == "无职英雄 ～技能什么的毫无用处～"
    assert post.title_en == "Mushoku no Eiyuu: Betsu ni Skill Nanka Iranakatta n da ga"
    assert post.episodes == "12"
    assert post.air_date == "2025年10月1日"
    assert post.air_weekday == "星期三"
    assert post.score == pytest.approx(4.9)
    assert post.score_text == "不过不失"
    assert post.index_tags == ["W", "WZ"]
    assert post.tags == ["轻改", "奇幻", "异世界", "龙傲天", "厕纸"]
    assert post.passwords == ["blackcatunderthemoon"]
    assert post.staff["director"] == "矢花馨"
    assert "pan.baidu.com" in post.links["baidu"]
    assert post.links["od_node"] == "https://od.catimage.work/"
    assert "女神所赐予的职业" in post.summary
    # 全频道公用的汇总表格不该进单帖记录
    assert "sheet" not in post.links
    assert post.has_photo


def test_shared_sheet_excluded(parsed) -> None:
    """1964 次出现的公用表格是频道级常量，不该被当成每帖资源。"""
    posts, _ = parsed
    leaked = [
        p.message_id
        for p in posts
        if "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX" in p.links.get("sheet", "")
    ]
    assert not leaked, f"公用表格泄进了 {len(leaked)} 条帖子"


def test_rejects_discussion_group_export(export_doc) -> None:
    """讨论群的帖子是转发副本、链接失效，必须拒绝导入。"""
    fake = {**export_doc, "type": "public_supergroup", "id": 1213081688}
    with pytest.raises(ExportMismatch):
        parse_export(fake, expected_channel_id=CHANNEL_ID)


def test_rejects_wrong_channel(export_doc) -> None:
    with pytest.raises(ExportMismatch):
        parse_export(export_doc, expected_channel_id=999)
