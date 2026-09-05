"""检索服务。

实测过的取舍：FTS5 的 trigram tokenizer 对中文 2 字查询（"英雄"、"无职"）
全部未命中——它要求查询至少 3 个字符。而 1639 行数据上 LIKE 全表扫只要 0.5~3ms，
rapidfuzz 对 1447 个标题模糊排序 10ms。所以这里就是 LIKE 预筛 + 打分精排，
没有倒排索引，也没有需要同步的影子列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from ..config import Settings
from ..domain.post import Post
from ..storage.repo import PostRepo

_HASHTAG = re.compile(r"#([0-9A-Za-z_一-鿿぀-ヿー]+)")

# 匹配层级的基础分。子串匹配永远排在模糊匹配之前，所以 fuzzy 的上限压在 400。
_EXACT, _PREFIX, _TITLE, _ALIAS, _BODY = 1000.0, 900.0, 800.0, 700.0, 500.0
_FUZZY_SCALE = 4.0


@dataclass(slots=True)
class SearchHit:
    post: Post
    score: float          # 命中词的平均匹配分
    hits: int             # 命中了几个查询词
    reason: str           # exact / prefix / title / alias / body / fuzzy / tag

    @property
    def sort_key(self) -> tuple[int, float]:
        """先比命中词数，再比匹配质量。两个维度不硬压成一个数。"""
        return (-self.hits, -self.score)


@dataclass(slots=True)
class SearchPage:
    """一页结果 + 分页所需的全部信息。

    `total` 是过滤后的真实总数，不是 `len(hits)` —— 分页按钮要靠它算边界，
    而「找到 8 条」和「找到 572 条只显示 8 条」对用户是完全不同的信息。
    """

    query: str
    hits: list[SearchHit]     # 当前页
    total: int                # 全部命中数
    page: int                 # 0-based
    page_size: int

    @property
    def pages(self) -> int:
        """总页数。空结果也算 1 页，避免调用方到处判 0。"""
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))

    @property
    def first_index(self) -> int:
        """当前页第一条在全局的序号（1-based），用于「3-10 / 共 572」这种显示。"""
        return self.page * self.page_size + 1

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.pages

    @property
    def is_empty(self) -> bool:
        return not self.hits


@dataclass(slots=True)
class Query:
    text: str
    terms: list[str]
    tags: list[str]

    @classmethod
    def parse(cls, raw: str) -> Query:
        """'#奇幻 无职英雄 技能' -> terms=['无职英雄','技能'], tags=['奇幻']

        必须按词拆：整串 LIKE '%无职英雄 技能%' 命中 0 条，逐词才各自命中 1 和 3 条。
        """
        tags = _HASHTAG.findall(raw)
        text = _HASHTAG.sub(" ", raw).strip()
        return cls(text=text, terms=text.split(), tags=tags)


def rank(post: Post, kw: str) -> tuple[float, str]:
    """一个打分函数取代多路分支：越精确的匹配得分越高，同层级里标题越短越靠前。"""
    if not kw:
        return 0.0, "tag"
    title = post.title_cn.lower()
    if title == kw:
        return _EXACT, "exact"
    if title.startswith(kw):
        return _PREFIX - min(len(title), 90), "prefix"
    pos = title.find(kw)
    if pos >= 0:
        return _TITLE - pos * 2 - len(title) * 0.1, "title"
    pos = post.search_title.lower().find(kw)
    if pos >= 0:
        return _ALIAS - pos, "alias"
    if kw in post.raw_text.lower():
        return _BODY, "body"
    return 0.0, "none"


def rank_terms(post: Post, terms: list[str]) -> tuple[float, int, str]:
    """多词打分：返回 (命中词的平均分, 命中词数, 匹配层级)。"""
    total = 0.0
    hits = 0
    reasons: list[str] = []
    for term in terms:
        score, reason = rank(post, term)
        if score > 0:
            total += score
            hits += 1
            reasons.append(reason)
    if hits == 0:
        return 0.0, 0, "none"
    return total / hits, hits, "+".join(dict.fromkeys(reasons))


class SearchService:
    def __init__(self, repo: PostRepo, settings: Settings) -> None:
        self._repo = repo
        self._cfg = settings

    async def search(self, raw: str, limit: int | None = None) -> list[SearchHit]:
        """取前 N 条。`/check` 和 CLI 用它，不需要知道分页。"""
        page = await self.search_page(raw, page_size=limit or self._cfg.search_page_size)
        return page.hits

    async def search_page(
        self, raw: str, *, page: int = 0, page_size: int | None = None
    ) -> SearchPage:
        """分页检索。

        每次翻页都重新查库和重新排序，不缓存结果集。理由：一次完整检索实测
        3~13ms（1639 行 LIKE + 200 个候选打分），而缓存要处理失效、内存增长、
        以及「翻页时帖子被编辑了」的一致性问题。省下 10ms 不值这些复杂度。

        排序是确定性的（`sort_key` 是全序），所以重新查得到的顺序完全一致，
        翻页不会出现重复或漏项。
        """
        size = page_size or self._cfg.search_page_size
        ranked = await self._rank_all(raw)
        total = len(ranked)
        # 越界的页码夹回最后一页，而不是返回空 —— 用户点了「末页」之后再点「下一页」
        # 不该看到空列表。
        last = max(0, -(-total // size) - 1) if total else 0
        page = max(0, min(page, last))
        start = page * size
        return SearchPage(
            query=raw,
            hits=ranked[start : start + size],
            total=total,
            page=page,
            page_size=size,
        )

    async def _rank_all(self, raw: str) -> list[SearchHit]:
        """完整的候选集，已排序。分页只是对它切片。"""
        q = Query.parse(raw)
        cap = self._cfg.search_max_candidates

        # 纯标签查询
        if q.tags and not q.terms:
            posts = await self._repo.by_tags(q.tags, limit=cap)
            return [SearchHit(p, 0.0, 0, "tag") for p in posts]

        if not q.terms:
            return []

        # 逐词取候选，并集。整串 LIKE 对多词查询是废的。
        cand: dict[tuple[int, int], Post] = {}
        for term in q.terms:
            for p in await self._repo.like_titles(term, cap):
                cand.setdefault(p.key, p)
        # 标题候选不足一页就扩到正文。用固定阈值而不是 limit：分页时 limit 是
        # 页大小，不该因为翻到第 3 页就改变候选集的构成。
        if len(cand) < self._cfg.search_page_size:
            for term in q.terms:
                for p in await self._repo.like_fulltext(term, cap):
                    cand.setdefault(p.key, p)

        terms_lower = [t.lower() for t in q.terms]
        hits: list[SearchHit] = []
        for p in cand.values():
            score, n, reason = rank_terms(p, terms_lower)
            if n:
                hits.append(SearchHit(p, score, n, reason))

        if not hits:   # 可能打错字，退到模糊匹配
            hits = await self._fuzzy(q.text.lower(), self._cfg.search_page_size)

        if q.tags:     # 标签当过滤器用
            want = set(q.tags)
            hits = [h for h in hits if want <= set(h.post.tags)]

        hits.sort(key=lambda h: h.sort_key)
        return hits

    async def check(self, raw: str) -> SearchHit | None:
        """判断这部番发过没有：搜索的特例，不是独立功能。"""
        hits = await self.search(raw, limit=1)
        return hits[0] if hits else None

    async def _fuzzy(self, kw: str, limit: int) -> list[SearchHit]:
        """错别字容错。WRatio 实测最稳：无值英雄→无职英雄、咒术回站→咒术回战、
        白色相薄2→白色相簿2 都排第一，而 ratio/QRatio 在第一个例子上直接跑偏。"""
        rows = await self._repo.all_titles()
        if not rows:
            return []
        choices = {i: title.lower() for i, (_, _, title) in enumerate(rows)}
        matched = process.extract(
            kw,
            choices,
            scorer=fuzz.WRatio,
            limit=limit * 3,
            score_cutoff=self._cfg.fuzzy_min_score,
        )
        wanted = [(rows[idx][0], rows[idx][1]) for _, _, idx in matched]
        by_key = {p.key: p for p in await self._repo.get_many(wanted)}
        out: list[SearchHit] = []
        for _, score, idx in matched:
            key = (rows[idx][0], rows[idx][1])
            if post := by_key.get(key):
                out.append(SearchHit(post, score * _FUZZY_SCALE, 1, "fuzzy"))
        return out
