"""检索层测试。盯住实测发现的两个真 bug 不复发：多词查询、模糊匹配跑偏。"""

from __future__ import annotations

import pytest

from animebot.config import Settings
from animebot.domain.post import Post
from animebot.search.service import Query, SearchService, rank, rank_terms
from animebot.storage.repo import PostRepo

from .conftest import make_post


class TestQueryParse:
    def test_splits_tags_and_terms(self) -> None:
        q = Query.parse("#奇幻 无职英雄 技能")
        assert q.tags == ["奇幻"]
        assert q.terms == ["无职英雄", "技能"]

    def test_tag_only(self) -> None:
        q = Query.parse("#奇幻 #异世界")
        assert q.tags == ["奇幻", "异世界"]
        assert q.terms == []

    def test_plain_text(self) -> None:
        q = Query.parse("无职英雄")
        assert q.tags == []
        assert q.terms == ["无职英雄"]


class TestRank:
    def test_exact_beats_prefix(self) -> None:
        exact, _ = rank(make_post(1, "英雄"), "英雄")
        prefix, _ = rank(make_post(2, "英雄教室"), "英雄")
        assert exact > prefix

    def test_prefix_beats_middle(self) -> None:
        prefix, _ = rank(make_post(1, "英雄教室"), "英雄")
        middle, _ = rank(make_post(2, "魔神英雄传"), "英雄")
        assert prefix > middle

    def test_shorter_title_wins_same_tier(self) -> None:
        short, _ = rank(make_post(1, "英雄教室"), "英雄")
        long, _ = rank(make_post(2, "英雄王，为了穷尽武道而转生～而后成为世界最强"), "英雄")
        assert short > long

    def test_alias_matches_english(self) -> None:
        p = make_post(1, "无职英雄", title_en="Mushoku no Eiyuu")
        score, reason = rank(p, "mushoku")
        assert score > 0
        assert reason == "alias"

    def test_body_fallback(self) -> None:
        p = make_post(1, "某番", raw_text="导演: 矢花馨")
        score, reason = rank(p, "矢花馨")
        assert score > 0
        assert reason == "body"

    def test_no_match(self) -> None:
        score, _ = rank(make_post(1, "某番"), "完全不相关")
        assert score == 0


class TestRankTerms:
    def test_more_hits_wins(self) -> None:
        """命中 2 词必须排在命中 1 词前面，即使后者单词得分更高。"""
        two = make_post(1, "无职英雄 ～技能什么的毫无用处～")
        one = make_post(2, "技能")
        _, n2, _ = rank_terms(two, ["无职英雄", "技能"])
        _, n1, _ = rank_terms(one, ["无职英雄", "技能"])
        assert n2 == 2
        assert n1 == 1


class TestSearchService:
    async def test_multi_term_query(self, repo: PostRepo, search: SearchService,
                                   sample_posts: list[Post]) -> None:
        """'无职英雄 技能' 整串 LIKE 命中 0，必须按词拆开搜。"""
        await repo.upsert_many(sample_posts)
        hits = await search.search("无职英雄 技能")
        assert hits
        assert hits[0].post.message_id == 1
        assert hits[0].hits == 2

    async def test_single_term(self, repo: PostRepo, search: SearchService,
                              sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hits = await search.search("英雄")
        titles = [h.post.title_cn for h in hits]
        assert "英雄教室" in titles

    async def test_typo_falls_back_to_fuzzy(self, repo: PostRepo, search: SearchService,
                                           sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hits = await search.search("咒术回站")
        assert hits
        assert hits[0].post.title_cn == "咒术回战"
        assert hits[0].reason == "fuzzy"

    async def test_typo_with_number_keeps_right_one(self, repo: PostRepo,
                                                   search: SearchService,
                                                   sample_posts: list[Post]) -> None:
        """'白色相薄2' 要排在 '白色相簿2' 而不是 '白色相簿'。"""
        await repo.upsert_many(sample_posts)
        hits = await search.search("白色相薄2")
        assert hits[0].post.title_cn == "白色相簿2"

    async def test_tag_only_query(self, repo: PostRepo, search: SearchService,
                                 sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hits = await search.search("#校园")
        titles = {h.post.title_cn for h in hits}
        assert titles == {"英雄教室", "白色相簿2"}

    async def test_tag_intersection(self, repo: PostRepo, search: SearchService,
                                   sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hits = await search.search("#奇幻 #异世界")
        assert [h.post.message_id for h in hits] == [1]

    async def test_tag_filters_keyword_results(self, repo: PostRepo, search: SearchService,
                                              sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hits = await search.search("#校园 英雄")
        assert [h.post.title_cn for h in hits] == ["英雄教室"]

    async def test_check_found(self, repo: PostRepo, search: SearchService,
                              sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        hit = await search.check("咒术回战")
        assert hit is not None
        assert hit.post.title_cn == "咒术回战"

    async def test_check_not_found(self, repo: PostRepo, search: SearchService,
                                  sample_posts: list[Post]) -> None:
        await repo.upsert_many(sample_posts)
        assert await search.check("完全不存在的番剧xyz") is None

    async def test_empty_query(self, search: SearchService) -> None:
        assert await search.search("") == []

    async def test_respects_limit(self, repo: PostRepo, search: SearchService) -> None:
        await repo.upsert_many([make_post(i, f"测试番剧{i}") for i in range(1, 21)])
        hits = await search.search("测试", limit=3)
        assert len(hits) == 3


class TestTitleMode:
    """默认标题搜索 + 全文回退。这批盯的是「标题命中绝不混全文」这条铁律。"""

    async def test_title_only_ignores_body_match(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """title_only=True：term 只在正文（导演名）里，不该命中。"""
        await repo.upsert_many([
            make_post(1, "某动画", raw_text="导演: 山田尚子"),
        ])
        hits = await search.search("山田尚子", title_only=True)
        # 标题域零命中 → 走 fuzzy 纠错，但绝不会给出 body 命中
        assert all(h.reason != "body" for h in hits)

    async def test_fulltext_finds_body_match(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """title_only=False（/s）：标题零命中才回退正文，能靠导演名找到。"""
        await repo.upsert_many([
            make_post(1, "某动画", raw_text="导演: 山田尚子"),
        ])
        hits = await search.search("山田尚子", title_only=False)
        assert hits
        assert hits[0].reason == "body"

    async def test_title_hit_never_mixes_body(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """核心：一个词既有标题命中又有正文命中时，全文模式也只返回标题命中。

        这是拆隐式级联的意义所在 —— 旧代码「标题不足一页就扩全文」会把正文命中
        混进来撑多结果、触发翻页。
        """
        await repo.upsert_many([
            make_post(1, "青春猪头少年"),                       # 标题命中
            make_post(2, "别的番", raw_text="讲一个青春的故事"),  # 只正文提到
        ])
        hits = await search.search("青春", title_only=False)
        ids = {h.post.message_id for h in hits}
        assert ids == {1}, "标题有命中时，正文命中不该混进来"

    async def test_fallback_flag_set_only_on_body_fallback(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """fell_back 标记：标题命中时 False，回退正文时 True。presenter 靠它打提示。"""
        await repo.upsert_many([
            make_post(1, "青春猪头少年"),
            make_post(2, "某动画", raw_text="导演: 山田尚子"),
        ])
        title_hit = await search.search_page("青春", title_only=False)
        assert title_hit.fallback is False

        body_hit = await search.search_page("山田尚子", title_only=False)
        assert body_hit.fallback is True

    async def test_title_mode_never_falls_back(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """title_only=True 永远不回退全文，fallback 恒 False。"""
        await repo.upsert_many([
            make_post(1, "某动画", raw_text="导演: 山田尚子"),
        ])
        page = await search.search_page("山田尚子", title_only=True)
        assert page.fallback is False

    async def test_title_mode_still_fuzzy_corrects(
        self, repo: PostRepo, search: SearchService, sample_posts: list[Post]
    ) -> None:
        """标题模式零命中仍走 fuzzy 纠错（纠错在标题域，不算回退）。"""
        await repo.upsert_many(sample_posts)
        hits = await search.search("咒术回站", title_only=True)
        assert hits
        assert hits[0].post.title_cn == "咒术回战"
        assert hits[0].reason == "fuzzy"

    async def test_page_carries_title_only(
        self, repo: PostRepo, search: SearchService
    ) -> None:
        """SearchPage 记住模式 —— 翻页 callback 靠它编码，翻页时结果集才不变。"""
        await repo.upsert_many([make_post(i, f"青春{i:03d}") for i in range(1, 30)])
        page = await search.search_page("青春", title_only=True)
        assert page.title_only is True


class TestPermalink:
    def test_public_mode(self, settings: Settings) -> None:
        p = make_post(3948, "某番")
        assert p.permalink(settings.link_username) == "https://t.me/YXHMd/3948"

    def test_internal_mode(self, settings: Settings) -> None:
        internal = settings.model_copy(update={"link_mode": "internal"})
        p = make_post(3948, "某番")
        assert p.permalink(internal.link_username) == \
            "https://t.me/c/1702674582/3948"

    @pytest.mark.parametrize("raw", [-1001702674582, 1702674582, -1702674582])
    def test_channel_id_normalized(self, raw: int) -> None:
        s = Settings(channel_id=raw)
        assert s.channel_id == 1702674582
        assert s.bot_api_chat_id == -1001702674582
