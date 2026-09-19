"""富列表 + 详情交互的单元测试。

盯住这次改动引入的、且不抛异常只是「显示错东西」的几类 bug：
  - 标题在列表里必须是纯文本（不能再是超链接，否则和详情按钮功能重复）
  - 真实直链按 URL 主机判（onedrive 指向 docs.google.com 的算跳转，不算直链）
  - 详情要有返回按钮，且返回目标带得回来源页
  - 详情按钮 callback 用 d:/D:，翻页用 s:/t:，不能串
"""

from __future__ import annotations

from animebot.config import Settings
from animebot.domain.links import DIRECT, INDEX, OFFICIAL, categorize, classify_link
from animebot.search.callbacks import MAX_CALLBACK_BYTES, QueryStore
from animebot.search.presenter import (
    detail_keyboard,
    format_hit_line,
    page_keyboard,
    render_page,
)
from animebot.search.service import SearchHit, SearchPage

from .conftest import make_post

LONG_TITLE = "英雄王，为了穷尽武道而转生～而后，成为世界最强的见习骑士♀～"


def _hit(mid: int, title: str, links: dict[str, str]) -> SearchHit:
    p = make_post(mid, title)
    p.links = links
    return SearchHit(p, 900.0, 1, "prefix")


def _page(hits: list[SearchHit], query: str = "测试", page: int = 0) -> SearchPage:
    return SearchPage(query=query, hits=hits, total=len(hits), page=page, page_size=8)


# ---------------------------------------------------------------- 链接分类


class TestClassify:
    def test_baidu_is_direct(self) -> None:
        assert classify_link("baidu", "https://pan.baidu.com/s/1abc?pwd=0000") == DIRECT

    def test_real_onedrive_is_direct(self) -> None:
        assert classify_link("onedrive", "https://p42k-my.sharepoint.com/:f:/g/x") == DIRECT

    def test_onedrive_pointing_to_sheet_is_index(self) -> None:
        """真实数据里 523 条这种：标签写 onedrive，其实指向共用表格。"""
        url = "https://docs.google.com/spreadsheets/d/1q2JyP/edit"
        assert classify_link("onedrive", url) == INDEX

    def test_gdrive_file_is_direct(self) -> None:
        """drive.google.com 是真实文件，不是 docs 表格。"""
        assert classify_link("gdrive", "https://drive.google.com/file/d/xyz/view") == DIRECT

    def test_sheet_kind_is_index(self) -> None:
        assert classify_link("sheet", "https://anything") == INDEX

    def test_node_is_index(self) -> None:
        assert classify_link("od_node", "https://od.catimage.work/") == INDEX

    def test_official_is_official(self) -> None:
        assert classify_link("official", "https://anime.example.com") == OFFICIAL

    def test_categorize_groups_and_orders(self) -> None:
        links = {
            "onedrive": "https://p42k-my.sharepoint.com/x",
            "baidu": "https://pan.baidu.com/s/1abc",
            "sheet": "https://docs.google.com/spreadsheets/d/1q2/edit",
        }
        g = categorize(links)
        # baidu 必须排在真实 onedrive 前面
        assert [k for k, _, _ in g[DIRECT]] == ["baidu", "onedrive"]
        assert [k for k, _, _ in g[INDEX]] == ["sheet"]

    def test_empty_url_skipped(self) -> None:
        g = categorize({"baidu": ""})
        assert g[DIRECT] == []


# ---------------------------------------------------------------- 列表行


_INTERNAL = Settings(link_mode="internal")


class TestHitLine:
    def test_title_is_hyperlink_to_post(self) -> None:
        """标题是跳原帖的超链接。internal 模式下走 t.me/c/ 私有链接。"""
        line = format_hit_line(1, _hit(5, "进击的巨人", {}), _INTERNAL, [])
        assert "进击的巨人" in line
        first = line.split("\n")[0]
        assert "<a href" in first, "标题应是超链接"
        assert "t.me/c/1702674582/5" in first, "internal 模式应是 /c/ 私有链接"

    def test_public_mode_uses_username_link(self) -> None:
        line = format_hit_line(1, _hit(5, "进击的巨人", {}), Settings(link_mode="public"), [])
        assert "t.me/YXHMd/5" in line.split("\n")[0]

    def test_direct_links_appended(self) -> None:
        line = format_hit_line(
            1,
            _hit(5, "进击的巨人", {"baidu": "https://pan.baidu.com/s/1a"}),
            _INTERNAL,
            [],
        )
        assert "百度网盘" in line
        assert 'href="https://pan.baidu.com/s/1a"' in line

    def test_index_links_not_in_list(self) -> None:
        """汇总表格/节点是全频道公用的，不该出现在每一行。"""
        line = format_hit_line(
            1,
            _hit(5, "x", {"sheet": "https://docs.google.com/spreadsheets/d/1q/edit"}),
            _INTERNAL,
            [],
        )
        assert "汇总表格" not in line
        assert "docs.google.com" not in line

    def test_keyword_highlighted_inside_link(self) -> None:
        """命中词的 <b> 嵌在标题 <a> 里 —— Telegram HTML 允许这种嵌套。"""
        line = format_hit_line(1, _hit(5, "无职英雄", {}), _INTERNAL, ["无职"])
        assert "<b>无职</b>" in line
        first = line.split("\n")[0]
        assert "<a href" in first and "<b>无职</b>" in first


# ---------------------------------------------------------------- 详情按钮


class TestDetailKeyboard:
    def test_direct_links_are_url_buttons(self) -> None:
        p = make_post(5, "x")
        p.links = {"baidu": "https://pan.baidu.com/s/1a", "gdrive": "https://drive.google.com/f"}
        kb = detail_keyboard(p, Settings())
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        assert "https://pan.baidu.com/s/1a" in urls
        assert "https://drive.google.com/f" in urls

    def test_has_original_post_button(self) -> None:
        kb = detail_keyboard(make_post(5, "x"), Settings())
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("原帖" in t for t in texts)

    def test_index_links_are_secondary_buttons(self) -> None:
        p = make_post(5, "x")
        p.links = {"sheet": "https://docs.google.com/spreadsheets/d/1q/edit"}
        kb = detail_keyboard(p, Settings())
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("汇总表格" in t for t in texts)


# ---------------------------------------------------------------- 键盘：只剩翻页


class TestPageKeyboardNav:
    def test_no_number_buttons(self) -> None:
        """序号详情按钮已删 —— 标题超链接直接跳原帖，不再要第二条路。"""
        store = QueryStore()
        page = _page([_hit(i, f"番{i}", {}) for i in (10, 20, 30)], "番", page=0)
        kb = page_keyboard(page, store)
        # 单页无翻页行 -> 整个键盘都没有
        assert kb is None, "单页结果不该有任何按钮"

    def test_only_nav_row_when_paged(self) -> None:
        store = QueryStore()
        hits = [_hit(i, f"番{i:03d}", {}) for i in range(1, 9)]
        page = SearchPage(query="番", hits=hits, total=25, page=0, page_size=8)
        kb = page_keyboard(page, store)
        assert kb is not None
        assert len(kb.inline_keyboard) == 1, "只该有翻页一行，没有序号按钮"
        assert [b.text for b in kb.inline_keyboard[0]] == ["·", "·", "1/4", "▶", "⏭"]

    def test_all_callbacks_within_limit(self) -> None:
        store = QueryStore()
        hits = [_hit(i, "番", {}) for i in range(1, 9)]
        page = SearchPage(query=LONG_TITLE + " x", hits=hits, total=25, page=1, page_size=8)
        kb = page_keyboard(page, store)
        assert kb is not None
        for row in kb.inline_keyboard:
            for b in row:
                if b.callback_data:
                    assert len(b.callback_data.encode()) <= MAX_CALLBACK_BYTES

    def test_list_render_title_links_to_post(self) -> None:
        page = _page([_hit(5, "进击的巨人", {"baidu": "https://pan.baidu.com/s/1a"})], "无关词")
        text = render_page(page, _INTERNAL)
        # 标题跳原帖（internal 走 /c/），直链单独是超链接
        assert "t.me/c/1702674582/5" in text, "标题应链到原帖"
        assert "进击的巨人" in text
        assert 'href="https://pan.baidu.com/s/1a">百度网盘' in text
