"""分页、callback 编解码、关键词高亮的测试。

这三块的 bug 有个共同特征：**不会抛异常，只会让界面显示错的东西**。
所以每一条都断言具体的字符串或数字，不满足于"没报错"。
"""

from __future__ import annotations

import html

import pytest

from animebot.config import Settings
from animebot.domain.post import Post
from animebot.search.callbacks import (
    MAX_CALLBACK_BYTES,
    NOOP,
    PREFIX_INLINE,
    PREFIX_TOKEN,
    QueryStore,
    decode_page,
    encode_page,
)
from animebot.search.highlight import find_spans, highlight, query_terms
from animebot.search.presenter import (
    page_keyboard,
    render_detail,
    render_page,
)
from animebot.search.service import SearchHit, SearchPage, SearchService
from animebot.storage.repo import PostRepo

from .conftest import make_post

# 库里真实存在的最长标题，30 个汉字 = 90 字节，内联必然超限
LONG_TITLE = "英雄王，为了穷尽武道而转生～而后，成为世界最强的见习骑士♀～"


# ---------------------------------------------------------------- 高亮


class TestHighlight:
    def test_escapes_when_no_match(self) -> None:
        assert highlight("a < b & c", []) == html.escape("a < b & c")

    def test_wraps_match(self) -> None:
        assert highlight("无职英雄", ["无职"]) == "<b>无职</b>英雄"

    def test_escape_before_tagging(self) -> None:
        """顺序错了就会输出字面 &lt;b&gt; 或在 &amp; 里找关键词。"""
        got = highlight("A & B", ["b"])
        assert got == "A &amp; <b>B</b>"

    def test_html_in_keyword_is_escaped(self) -> None:
        """标题里带 < > 的情况：高亮的那一段也必须转义。"""
        got = highlight("<script>", ["<script>"])
        assert got == "<b>&lt;script&gt;</b>"
        assert "<script>" not in got.replace("<b>", "").replace("</b>", "")

    def test_case_insensitive(self) -> None:
        assert highlight("Mushoku no Eiyuu", ["mushoku"]) == "<b>Mushoku</b> no Eiyuu"

    def test_overlapping_terms_merged(self) -> None:
        """不合并会插出 <b><b>无职</b>英雄</b> 这种嵌套。"""
        got = highlight("无职英雄", ["无职", "无职英雄"])
        assert got == "<b>无职英雄</b>"
        assert got.count("<b>") == 1

    def test_adjacent_terms(self) -> None:
        got = highlight("无职英雄", ["无职", "英雄"])
        assert got == "<b>无职英雄</b>", f"相邻区间没合并: {got}"

    def test_multiple_occurrences(self) -> None:
        got = highlight("英雄与英雄", ["英雄"])
        assert got == "<b>英雄</b>与<b>英雄</b>"

    def test_length_changing_lowercase_does_not_corrupt(self) -> None:
        """'İ'.lower() 是 2 个字符 —— 用 lower() 的偏移切原文会错位。"""
        text = "İstanbul 英雄"
        got = highlight(text, ["英雄"])
        assert got == "İstanbul <b>英雄</b>"

    def test_empty_term_ignored(self) -> None:
        assert highlight("英雄", [""]) == "英雄"

    def test_mark_cap(self) -> None:
        """命中几百次不该把消息撑爆。"""
        spans = find_spans("啊" * 500, ["啊"])
        assert len(spans) <= 40

    def test_query_terms_drops_tags(self) -> None:
        """标签是筛选条件，不出现在标题里，高亮它只是噪音。"""
        assert query_terms("#奇幻 无职英雄 技能") == ["无职英雄", "技能"]
        assert query_terms("#奇幻 #异世界") == []


# ---------------------------------------------------------------- callback 编解码


class TestCallbackCodec:
    def test_short_query_inlined(self) -> None:
        store = QueryStore()
        data = encode_page(2, "无职英雄", store, title_only=False)
        assert data.startswith(f"{PREFIX_INLINE}:")
        assert len(store) == 0, "短查询词不该占用暂存"

    def test_roundtrip_inline(self) -> None:
        store = QueryStore()
        ref = decode_page(encode_page(3, "#奇幻 无职英雄", store, title_only=False), store)
        assert ref is not None
        assert ref.page == 3
        assert ref.query == "#奇幻 无职英雄"

    def test_query_with_colon_survives(self) -> None:
        """'Re：从零开始' 这类标题带冒号，而 callback_data 用冒号分隔。

        模式位插在 page 和 query 之间，query 仍是 split(":", 3) 的最后一段，
        所以内部冒号照样活着 —— 这条是那次改格式最容易踩的回归。
        """
        store = QueryStore()
        q = "Re:从零开始:测试"
        ref = decode_page(encode_page(1, q, store, title_only=False), store)
        assert ref is not None
        assert ref.query == q

    def test_long_query_uses_token(self) -> None:
        store = QueryStore()
        data = encode_page(0, LONG_TITLE, store, title_only=False)
        assert data.startswith(f"{PREFIX_TOKEN}:")
        assert len(data.encode()) <= MAX_CALLBACK_BYTES
        assert len(store) == 1

    def test_roundtrip_token(self) -> None:
        store = QueryStore()
        ref = decode_page(encode_page(5, LONG_TITLE, store, title_only=False), store)
        assert ref is not None
        assert ref.page == 5
        assert ref.query == LONG_TITLE

    def test_mode_bit_roundtrips_inline(self) -> None:
        """模式位必须原样往返 —— 否则 /s 全文翻页会被当标题模式重查、结果集变。"""
        store = QueryStore()
        title = decode_page(encode_page(1, "青春", store, title_only=True), store)
        full = decode_page(encode_page(1, "青春", store, title_only=False), store)
        assert title is not None and title.title_only is True
        assert full is not None and full.title_only is False

    def test_mode_bit_roundtrips_token(self) -> None:
        """长查询走 token 时模式位一样要活着。"""
        store = QueryStore()
        ref = decode_page(encode_page(1, LONG_TITLE, store, title_only=True), store)
        assert ref is not None
        assert ref.title_only is True

    @pytest.mark.parametrize("page", [0, 1, 9, 99, 999])
    @pytest.mark.parametrize("title_only", [True, False])
    def test_never_exceeds_limit(self, page: int, title_only: bool) -> None:
        """任何页码 + 任何长度的查询词 + 任一模式，编出来都不能超 64 字节。

        加模式位是 +2 字节（':t'/':f'），token 形态最坏也就 't:999:t:xxxxxxxx'
        = 15 字节，离 64 还远。这条把「加模式位后是否仍 ≤64」钉死。
        """
        store = QueryStore()
        for q in ("英雄", LONG_TITLE, "啊" * 200, "#奇幻 #异世界 #轻改 #龙傲天 #厕纸"):
            data = encode_page(page, q, store, title_only=title_only)
            assert len(data.encode()) <= MAX_CALLBACK_BYTES, f"{q[:20]!r} 超限"

    def test_same_query_same_token(self) -> None:
        store = QueryStore()
        a = encode_page(0, LONG_TITLE, store, title_only=False)
        b = encode_page(1, LONG_TITLE, store, title_only=False)
        assert a.rsplit(":", 1)[1] == b.rsplit(":", 1)[1]
        assert len(store) == 1

    def test_expired_token_returns_none(self) -> None:
        """LRU 淘汰后必须返回 None，让 handler 给出"搜索已过期"而不是搜错东西。"""
        store = QueryStore(capacity=2)
        data = encode_page(0, LONG_TITLE, store, title_only=False)
        encode_page(0, LONG_TITLE + "A", store, title_only=False)
        encode_page(0, LONG_TITLE + "B", store, title_only=False)
        assert decode_page(data, store) is None

    def test_lru_keeps_recently_used(self) -> None:
        store = QueryStore(capacity=2)
        first = encode_page(0, LONG_TITLE, store, title_only=False)
        encode_page(0, LONG_TITLE + "A", store, title_only=False)
        decode_page(first, store)                     # 摸一下，变成最近使用
        encode_page(0, LONG_TITLE + "B", store, title_only=False)  # 该淘汰 A 而非 first
        assert decode_page(first, store) is not None

    @pytest.mark.parametrize(
        "bad",
        # 少段、页码非数字、模式位非法、前缀未知 —— 都得挡住
        ["", "s", "s:", "s:1", "s:1:f", "s:abc:f:x", "s:1:z:x", NOOP,
         "zzz:1:f:x", "s:-1:f:x"],
    )
    def test_malformed_returns_none(self, bad: str) -> None:
        assert decode_page(bad, QueryStore()) is None


# ---------------------------------------------------------------- 分页逻辑


def many_posts(n: int) -> list[Post]:
    return [make_post(i, f"测试番剧{i:03d}") for i in range(1, n + 1)]


class TestSearchPage:
    async def test_first_page(self, repo: PostRepo, search: SearchService) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page_size=8)
        assert len(page.hits) == 8
        assert page.total == 25
        assert page.pages == 4
        assert page.page == 0
        assert page.first_index == 1
        assert not page.has_prev
        assert page.has_next

    async def test_middle_page(self, repo: PostRepo, search: SearchService) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=1, page_size=8)
        assert page.first_index == 9
        assert page.has_prev
        assert page.has_next

    async def test_last_page_partial(self, repo: PostRepo, search: SearchService) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=3, page_size=8)
        assert len(page.hits) == 1
        assert page.first_index == 25
        assert page.has_prev
        assert not page.has_next

    async def test_no_overlap_or_gap_across_pages(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """翻页必须既不重复也不漏。排序是全序才能保证这点。"""
        await repo.upsert_many(many_posts(25))
        seen: list[int] = []
        for i in range(4):
            page = await search.search_page("测试番剧", page=i, page_size=8)
            seen.extend(h.post.message_id for h in page.hits)
        assert len(seen) == 25
        assert len(set(seen)) == 25, "翻页出现重复"

    async def test_repeated_query_stable_order(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """每次翻页都重查库，所以顺序必须是确定的。"""
        await repo.upsert_many(many_posts(25))
        a = await search.search_page("测试番剧", page=1, page_size=8)
        b = await search.search_page("测试番剧", page=1, page_size=8)
        assert [h.post.message_id for h in a.hits] == [h.post.message_id for h in b.hits]

    async def test_page_beyond_end_clamps(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """点了末页再点下一页不该看到空列表。"""
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=999, page_size=8)
        assert page.page == 3
        assert len(page.hits) == 1

    async def test_negative_page_clamps(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=-5, page_size=8)
        assert page.page == 0

    async def test_empty_result(self, search: SearchService) -> None:
        page = await search.search_page("完全不存在xyz")
        assert page.is_empty
        assert page.total == 0
        assert page.pages == 1          # 空结果也算 1 页，调用方不必判 0
        assert not page.has_prev
        assert not page.has_next

    async def test_exact_multiple_page_size(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """16 条 / 每页 8 = 正好 2 页，不该多出一个空页。"""
        await repo.upsert_many(many_posts(16))
        page = await search.search_page("测试番剧", page_size=8)
        assert page.pages == 2
        last = await search.search_page("测试番剧", page=1, page_size=8)
        assert len(last.hits) == 8
        assert not last.has_next

    async def test_total_is_real_not_capped(
        self, repo: PostRepo, settings: Settings
    ) -> None:
        """total 是用户可见的数字，不能被候选集上限截断成谎话。"""
        await repo.upsert_many(many_posts(300))
        svc = SearchService(repo, settings)
        page = await svc.search_page("测试番剧", page_size=8)
        assert page.total == 300

    async def test_search_still_returns_first_page(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """老的 search() 签名不能变 —— /check 和 CLI 都在用。"""
        await repo.upsert_many(many_posts(25))
        hits = await search.search("测试番剧", limit=3)
        assert len(hits) == 3


# ---------------------------------------------------------------- 渲染


class TestRenderPage:
    async def test_shows_total_and_range(
        self, repo: PostRepo, search: SearchService, settings: Settings
    ) -> None:
        """「找到 8 条」和「共 25 条只显示 8 条」对用户是完全不同的信息。"""
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=1, page_size=8)
        text = render_page(page, settings)
        assert "共 25 条" in text
        assert "9-16" in text
        assert "第 2/4 页" in text

    async def test_index_is_global(
        self, repo: PostRepo, search: SearchService, settings: Settings
    ) -> None:
        """第 2 页的序号是 9~16，不是 1~8 —— 和按钮一致，用户不用心算。"""
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=1, page_size=8)
        text = render_page(page, settings)
        assert "\n9. " in f"\n{text}"
        assert "16. " in text

    async def test_keyword_highlighted(
        self, repo: PostRepo, search: SearchService, settings: Settings,
        sample_posts: list[Post],
    ) -> None:
        await repo.upsert_many(sample_posts)
        page = await search.search_page("无职")
        text = render_page(page, settings)
        assert "<b>无职</b>" in text

    async def test_tag_not_highlighted(
        self, repo: PostRepo, search: SearchService, settings: Settings,
        sample_posts: list[Post],
    ) -> None:
        await repo.upsert_many(sample_posts)
        page = await search.search_page("#奇幻")
        text = render_page(page, settings)
        assert "<b>奇幻</b>" not in text

    async def test_empty_page_gives_hint(
        self, search: SearchService, settings: Settings
    ) -> None:
        page = await search.search_page("完全不存在xyz")
        text = render_page(page, settings)
        assert "没找到" in text
        assert "#奇幻" in text, "空结果应该给出可操作的建议"

    async def test_query_escaped_in_header(
        self, search: SearchService, settings: Settings
    ) -> None:
        page = await search.search_page("<script>")
        text = render_page(page, settings)
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    async def test_fallback_hint_shown(
        self, repo: PostRepo, search: SearchService, settings: Settings
    ) -> None:
        """标题零命中回退全文时，顶部要有「没匹配到标题」提示，别让用户以为真有这番。"""
        await repo.upsert_many([make_post(1, "某动画", raw_text="导演: 山田尚子")])
        page = await search.search_page("山田尚子", title_only=False)
        text = render_page(page, settings)
        assert page.fallback is True
        assert "没匹配到标题" in text
        assert "相关内容" in text

    async def test_no_fallback_hint_on_title_hit(
        self, repo: PostRepo, search: SearchService, settings: Settings
    ) -> None:
        """标题有命中就绝不打提示行 —— 那行只属于回退场景。"""
        await repo.upsert_many([make_post(1, "青春猪头少年")])
        page = await search.search_page("青春", title_only=False)
        text = render_page(page, settings)
        assert "没匹配到标题" not in text

    async def test_fallback_hint_query_escaped(
        self, settings: Settings
    ) -> None:
        """回退提示里也带查询词，一样要转义，不能开 XSS 口子。"""
        page = SearchPage(
            query="<b>x</b>",
            hits=[SearchHit(make_post(1, "某番", raw_text="x"), 500.0, 1, "body")],
            total=1, page=0, page_size=8, title_only=False, fallback=True,
        )
        text = render_page(page, settings)
        assert "没匹配到标题" in text
        assert "<b>x</b>" not in text.replace("没匹配到标题", "")  # 查询词本身不该原样出现
        assert "&lt;b&gt;x&lt;/b&gt;" in text


class TestPageKeyboard:
    async def test_single_page_no_keyboard(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """单页结果没有任何按钮 —— 序号详情按钮已删，翻页行也不需要。"""
        await repo.upsert_many(many_posts(5))
        page = await search.search_page("测试番剧", page_size=8)
        assert page_keyboard(page, QueryStore()) is None

    async def test_nav_row_present_when_paged(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page_size=8)
        kb = page_keyboard(page, QueryStore())
        assert kb is not None
        assert len(kb.inline_keyboard) == 1, "只有翻页一行，没有序号按钮"
        nav = kb.inline_keyboard[-1]
        assert [b.text for b in nav] == ["·", "·", "1/4", "▶", "⏭"]

    async def test_nav_row_width_constant(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """到边界的按钮变占位而不是消失 —— 否则按钮位置会左右跳，用户点错。"""
        await repo.upsert_many(many_posts(25))
        widths = set()
        for i in range(4):
            page = await search.search_page("测试番剧", page=i, page_size=8)
            kb = page_keyboard(page, QueryStore())
            assert kb is not None
            widths.add(len(kb.inline_keyboard[-1]))
        assert widths == {5}, f"翻页行宽度变了: {widths}"

    async def test_middle_page_all_enabled(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=1, page_size=8)
        kb = page_keyboard(page, QueryStore())
        assert kb is not None
        assert [b.text for b in kb.inline_keyboard[-1]] == ["⏮", "◀", "2/4", "▶", "⏭"]

    async def test_last_page_next_disabled(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=3, page_size=8)
        kb = page_keyboard(page, QueryStore())
        assert kb is not None
        assert [b.text for b in kb.inline_keyboard[-1]] == ["⏮", "◀", "4/4", "·", "·"]

    async def test_nav_targets_correct(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        store = QueryStore()
        page = await search.search_page("测试番剧", page=1, page_size=8)
        kb = page_keyboard(page, store)
        assert kb is not None
        first, prev, _label, nxt, last = kb.inline_keyboard[-1]
        assert decode_page(first.callback_data or "", store).page == 0     # type: ignore[union-attr]
        assert decode_page(prev.callback_data or "", store).page == 0      # type: ignore[union-attr]
        assert decode_page(nxt.callback_data or "", store).page == 2       # type: ignore[union-attr]
        assert decode_page(last.callback_data or "", store).page == 3      # type: ignore[union-attr]

    async def test_disabled_buttons_use_noop(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """占位按钮必须有 callback_data，否则 Telegram 拒绝整个键盘。"""
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page_size=8)
        kb = page_keyboard(page, QueryStore())
        assert kb is not None
        for b in kb.inline_keyboard[-1]:
            assert b.callback_data, f"按钮 {b.text!r} 没有 callback_data"
        assert kb.inline_keyboard[-1][0].callback_data == NOOP

    async def test_no_index_buttons_only_nav(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """序号按钮已删：键盘只剩翻页那一行。"""
        await repo.upsert_many(many_posts(25))
        page = await search.search_page("测试番剧", page=1, page_size=8)
        kb = page_keyboard(page, QueryStore())
        assert kb is not None
        assert len(kb.inline_keyboard) == 1, "不该再有序号按钮行"

    async def test_all_callback_data_within_limit(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        await repo.upsert_many(many_posts(25))
        store = QueryStore()
        page = await search.search_page(LONG_TITLE + " 测试番剧", page=1, page_size=8)
        kb = page_keyboard(page, store)
        if kb is None:
            pytest.skip("这个查询没有结果")
        for row in kb.inline_keyboard:
            for b in row:
                if b.callback_data:
                    assert len(b.callback_data.encode()) <= MAX_CALLBACK_BYTES

    async def test_empty_page_no_keyboard(
        self, search: SearchService
    ) -> None:
        page = await search.search_page("完全不存在xyz")
        assert page_keyboard(page, QueryStore()) is None


class TestRenderDetail:
    def test_highlights_terms(self) -> None:
        """标题加粗行里命中词用下划线（bold-on-bold 看不出），正文里用加粗。"""
        p = make_post(1, "无职英雄", summary="一个无职之人的故事")
        text = render_detail(p, ["无职"])
        assert "<u>无职</u>" in text, "标题里的命中词应下划线高亮"
        assert "<b>无职</b>" in text, "正文里的命中词应加粗高亮"

    def test_no_terms_still_escapes(self) -> None:
        p = make_post(1, "A & B")
        assert "A &amp; B" in render_detail(p)
