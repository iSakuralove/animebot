"""给模型用的工具。

四个，都是「模型自己查不到、但用户会问」的东西：

- `bangumi_search` —— 按名字找番剧，拿 id 和评分。
- `bangumi_detail` —— 按 id 拿完整条目：简介、制作人员、话数、评分分布。
- `bangumi_browse` —— 按标签 / 年份 / 评分区间列榜单。回答「2026 年有什么穿越番」
  「近十年评分最高的动画」这类**没有具体片名**的问题，靠 search 是做不到的。
- `web_search` —— 联网搜时效性信息（网关会静默丢弃服务端工具，只能我们搜）。
- `channel_search` —— 查我们自己的索引（这个 bot 独有的数据）。

Bangumi 的三个工具共用 `/v0/search/subjects` 和 `/v0/subjects/{id}`，带
`Authorization: Bearer <token>`（限流更宽）。实测（2026-10-29）：

- `limit` 服务端硬顶 20，传 100 也只回 20 —— 不做客户端翻页，够用；
- `sort=score` 会把「1 个人打 10 分」的冷门条目顶到第一，**必须配
  `rating_count` 下限**，否则「评分最高」是个笑话；
- `sort=rank` 会把未排名条目（rank=0）排在最前，同样要 `rank: [">=1"]`；
- 搜索接口 schema 要求 `keyword` 必填，按条件浏览时传空串。

工具返回一律是 JSON 字符串，出错时返回 `{"error": ...}` 而不是抛异常 ——
一次工具失败不该让整轮对话崩掉。
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
import urllib.parse
from typing import Any

import aiohttp

from ...config import Settings
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from ...search.service import SearchService

log = get_logger("animebot.ai.tools")

_BGM_BASE = "https://api.bgm.tv"
_BGM_SEARCH = f"{_BGM_BASE}/v0/search/subjects"
_BGM_UA = "animebot/0.1 (https://github.com/iSakuralove/animebot)"
_BGM_TYPES = {"anime": 2, "book": 1, "music": 3, "game": 4}
_BGM_PAGE_MAX = 20          # 服务端上限，传更大也只回 20
_BGM_TTL = 600.0            # 番剧元数据十分钟内不会变

#: 拿来做系统提示里「近十年」这类相对时间的锚点。
_CACHE_MAX = 256

_BING_URL = "https://www.bing.com/search"
_BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BLOCK = re.compile(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', re.S)
_H2 = re.compile(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)
_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")

#: 详情里值得回灌给模型的 infobox 字段。其余（价格、ISBN、页数）是噪音。
_INFOBOX_KEYS = (
    "中文名", "别名", "话数", "放送开始", "放送星期", "导演", "原作", "脚本",
    "系列构成", "动画制作", "制作", "音乐", "官方网站", "播放结束",
)


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment)).strip()


def unwrap_bing(url: str) -> str:
    """Bing 的结果链接是 `bing.com/ck/a?...&u=a1<base64>` 跳转，解出真实地址。

    解不出来就原样返回 —— 给模型一个能点的 Bing 链接也比丢结果强。
    """
    raw = html.unescape(url)
    parsed = urllib.parse.urlparse(raw)
    if "bing.com" not in parsed.netloc:
        return raw
    target = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
    if not target.startswith("a1"):
        return raw
    token = target[2:]
    token += "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token).decode("utf-8", "ignore") or raw
    except (ValueError, TypeError):
        return raw


def parse_bing(page: str, limit: int = 5) -> list[dict[str, str]]:
    """从 Bing 结果页抽 (标题, 链接, 摘要)。解析失败返回空列表，不抛。"""
    out: list[dict[str, str]] = []
    for block in _BLOCK.findall(page)[:limit]:
        head = _H2.search(block)
        if not head:
            continue
        url = unwrap_bing(head.group(1))
        title = _text(head.group(2))
        snip = _SNIPPET.search(block)
        out.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(snip.group(1))[:400] if snip else "",
            }
        )
    return out


def _infobox(items: Any) -> dict[str, str]:
    """infobox 是 [{key, value}]，value 可能是字符串或 [{v: ...}] 列表。"""
    out: dict[str, str] = {}
    for entry in items or []:
        key = entry.get("key")
        if key not in _INFOBOX_KEYS:
            continue
        value = entry.get("value")
        if isinstance(value, list):
            text = "、".join(str(v.get("v", "")) for v in value if isinstance(v, dict))
        else:
            text = str(value or "")
        if text:
            out[key] = text[:200]
    return out


def _subject_row(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """搜索结果 -> 回灌给模型的紧凑形状。空字段直接不出现，省 token。"""
    rating = item.get("rating") or {}
    row: dict[str, Any] = {
        "id": item.get("id"),
        "name": item.get("name"),
        "name_cn": item.get("name_cn") or "",
        "date": item.get("date"),
        "score": rating.get("score"),
        "rank": rating.get("rank") or None,
        "votes": rating.get("total"),
        "url": f"https://bgm.tv/subject/{item.get('id')}",
    }
    if detail:
        row.pop("url", None)
        row["platform"] = item.get("platform")
        row["episodes"] = item.get("total_episodes") or item.get("eps")
        row["tags"] = [t.get("name") for t in (item.get("tags") or [])[:10]]
        row["staff"] = _infobox(item.get("infobox"))
        row["summary"] = (item.get("summary") or "").strip()[:600]
        collection = item.get("collection") or {}
        if collection:
            row["collection"] = {
                k: collection.get(k) for k in ("wish", "collect", "doing", "dropped")
            }
        row["rating_dist"] = rating.get("count") or None
    return {k: v for k, v in row.items() if v not in (None, "", [], {})}


class ToolBox:
    """工具的声明 + 执行。一个实例全局共用一个 aiohttp session。"""

    def __init__(
        self,
        settings: Settings,
        *,
        searches: dict[str, SearchService] | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._cfg = settings
        self._searches = searches or {}
        self._session = session
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---------------------------------------------------------- 声明
    @staticmethod
    def specs() -> list[dict[str, Any]]:
        return [
            {
                "name": "bangumi_search",
                "description": (
                    "按名字在 Bangumi（bgm.tv）搜索条目，返回 id、中日文名、评分、排名、"
                    "评分人数、放送日期。用户提到具体作品名时先用它；要简介或制作人员"
                    "再拿返回的 id 调 bangumi_detail。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "作品名，中/日/英文均可"},
                        "type": {
                            "type": "string",
                            "enum": ["anime", "book", "music", "game"],
                            "description": "条目类型，默认 anime",
                        },
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "bangumi_detail",
                "description": (
                    "按 Bangumi 条目 id 取完整信息：简介（讲了什么）、话数、放送日期、"
                    "导演 / 原作 / 动画制作、热门标签、评分分布。用户在问「这部讲什么」"
                    "时必须调它，不要凭记忆编简介。id 来自 bangumi_search 或 bangumi_browse。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject_id": {"type": "integer", "description": "Bangumi 条目 id"},
                    },
                    "required": ["subject_id"],
                },
            },
            {
                "name": "bangumi_browse",
                "description": (
                    "按条件列 Bangumi 榜单，用于**没有具体片名**的推荐 / 排行类问题："
                    "「2026 年有什么穿越番」「近十年评分最高的动画」「今年七月番有哪些」。"
                    "按评分排序时务必给 min_votes，否则会把只有一两个人打分的冷门条目"
                    "排在前面。按排名排序时给 max_rank（如 100）。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "标签，多个是「且」关系。"
                                           '如 ["穿越"]、["百合","校园"]',
                        },
                        "year_from": {"type": "integer", "description": "起始年份，含"},
                        "year_to": {"type": "integer", "description": "结束年份，含"},
                        "min_score": {"type": "number", "description": "最低评分，如 7.5"},
                        "min_votes": {
                            "type": "integer",
                            "description": "最少评分人数。按评分排序时建议 >=500；不熟悉时用 1000",
                        },
                        "max_rank": {
                            "type": "integer",
                            "description": "排名上限。问「评分最高」时给 100 或 200",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["rank", "score", "heat"],
                            "description": (
                                "rank=按排名（最稳），score=按评分"
                                "（需配 min_votes），heat=按收藏人数"
                            ),
                        },
                        "type": {
                            "type": "string",
                            "enum": ["anime", "book", "music", "game"],
                            "description": "条目类型，默认 anime",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数，最多 20，默认 10",
                        },
                    },
                },
            },
            {
                "name": "web_search",
                "description": (
                    "联网搜索网页，返回标题、链接和摘要。用于 Bangumi 查不到的时效性"
                    "信息（新番情报、新闻、访谈）或需要多个来源佐证时。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "channel_search",
                "description": (
                    "在本 bot 索引的两个 Telegram 频道里查帖子。用户问「频道发过没」"
                    "「有没有资源」「求下载」时用它。返回标题、原帖链接、评分、标签。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "作品名或关键词"},
                        "channel": {
                            "type": "string",
                            "enum": ["anime", "manga"],
                            "description": "anime = 动漫频道，manga = 漫画频道；默认两个都查",
                        },
                    },
                    "required": ["keyword"],
                },
            },
        ]

    # ---------------------------------------------------------- 执行
    async def run(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "bangumi_search":
                data = await self.bangumi_search(
                    str(args.get("keyword", "")), str(args.get("type", "anime"))
                )
            elif name == "bangumi_detail":
                data = await self.bangumi_detail(args.get("subject_id"))
            elif name == "bangumi_browse":
                data = await self.bangumi_browse(
                    tags=args.get("tags"),
                    year_from=args.get("year_from"),
                    year_to=args.get("year_to"),
                    min_score=args.get("min_score"),
                    min_votes=args.get("min_votes"),
                    max_rank=args.get("max_rank"),
                    sort=str(args.get("sort") or "rank"),
                    kind=str(args.get("type") or "anime"),
                    limit=args.get("limit"),
                )
            elif name == "web_search":
                data = await self.web_search(str(args.get("query", "")))
            elif name == "channel_search":
                data = await self.channel_search(
                    str(args.get("keyword", "")), args.get("channel")
                )
            else:
                data = {"error": f"没有这个工具: {name}"}
            outcome = "error" if isinstance(data, dict) and data.get("error") else "ok"
        except Exception as exc:
            outcome = "error"
            log.warning("ai.tool_failed", tool=name, error=repr(exc))
            data = {"error": f"{type(exc).__name__}: {exc}"}
        METRICS.incr("ai.tool", tool=name, outcome=outcome)
        return json.dumps(data, ensure_ascii=False)

    # ---------------------------------------------------------- Bangumi
    async def bangumi_search(self, keyword: str, kind: str = "anime") -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            return {"error": "keyword 不能为空"}
        body = {
            "keyword": keyword,
            "filter": {"type": [_BGM_TYPES.get(kind, 2)], "nsfw": False},
        }
        payload = await self._bgm_post(body, limit=10)
        if "error" in payload:
            return payload
        items = payload.get("data") or []
        return {
            "keyword": keyword,
            "total": payload.get("total", len(items)),
            "results": [_subject_row(x) for x in items],
        }

    async def bangumi_detail(self, subject_id: Any) -> dict[str, Any]:
        try:
            sid = int(subject_id)
        except (TypeError, ValueError):
            return {"error": "subject_id 必须是整数（来自 bangumi_search 的 id 字段）"}
        payload = await self._bgm_get(f"{_BGM_BASE}/v0/subjects/{sid}")
        if "error" in payload:
            return payload
        return _subject_row(payload, detail=True)

    async def bangumi_browse(
        self,
        *,
        tags: Any = None,
        year_from: Any = None,
        year_to: Any = None,
        min_score: Any = None,
        min_votes: Any = None,
        max_rank: Any = None,
        sort: str = "rank",
        kind: str = "anime",
        limit: Any = None,
    ) -> dict[str, Any]:
        """条件浏览。每个参数都直接映射到 Bangumi 的 filter 语法。"""
        filters: dict[str, Any] = {"type": [_BGM_TYPES.get(kind, 2)], "nsfw": False}
        if isinstance(tags, str):
            tags = [tags]
        if tags:
            filters["tag"] = [str(t) for t in tags if str(t).strip()][:4]

        dates: list[str] = []
        if year_from:
            dates.append(f">={int(year_from)}-01-01")
        if year_to:
            dates.append(f"<{int(year_to) + 1}-01-01")
        if dates:
            filters["air_date"] = dates

        if min_score:
            filters["rating"] = [f">={float(min_score)}"]
        if min_votes:
            filters["rating_count"] = [f">={int(min_votes)}"]
        if max_rank:
            # rank 是 1-based 排名，0 表示「还没排上」，必须排除 ——
            # 否则 sort=rank 会把这些条目排在最前面（实测）。
            filters["rank"] = [">=1", f"<={int(max_rank)}"]

        if sort in ("score", "heat") and not min_votes:
            # 按评分排序却不设人数下限 = 把 1 人打 10 分的冷门条目排第一。
            filters["rating_count"] = [">=300"]

        capped = min(max(int(limit or 10), 1), _BGM_PAGE_MAX)
        payload = await self._bgm_post(
            {"keyword": "", "sort": sort if sort in ("rank", "score", "heat") else "rank",
             "filter": filters},
            limit=capped,
        )
        if "error" in payload:
            return payload
        items = payload.get("data") or []
        return {
            "query": {
                "tags": filters.get("tag", []),
                "air_date": filters.get("air_date", []),
                "sort": sort,
                "filters": {k: v for k, v in filters.items() if k not in ("type", "nsfw")},
            },
            "total_matched": payload.get("total", len(items)),
            "results": [_subject_row(x) for x in items],
        }

    async def _bgm_post(self, body: dict[str, Any], *, limit: int) -> dict[str, Any]:
        key = "post:" + json.dumps({**body, "_limit": limit}, ensure_ascii=False, sort_keys=True)
        if (hit := self._cache_get(key)) is not None:
            return hit
        session = await self._open()
        try:
            async with session.post(
                f"{_BGM_SEARCH}?limit={min(limit, _BGM_PAGE_MAX)}",
                json=body,
                headers=self._bgm_headers(json_body=True),
            ) as resp:
                if resp.status >= 400:
                    return {"error": f"Bangumi HTTP {resp.status}"}
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            return {"error": f"Bangumi 访问失败: {type(exc).__name__}"}
        self._cache_put(key, payload)
        return payload

    async def _bgm_get(self, url: str) -> dict[str, Any]:
        if (hit := self._cache_get("get:" + url)) is not None:
            return hit
        session = await self._open()
        try:
            async with session.get(url, headers=self._bgm_headers()) as resp:
                if resp.status == 404:
                    return {"error": "Bangumi 上没有这个条目 id"}
                if resp.status >= 400:
                    return {"error": f"Bangumi HTTP {resp.status}"}
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            return {"error": f"Bangumi 访问失败: {type(exc).__name__}"}
        self._cache_put("get:" + url, payload)
        return payload

    def _bgm_headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"User-Agent": _BGM_UA, "Accept": "application/json"}
        if json_body:
            headers["content-type"] = "application/json"
        token = self._cfg.bgm_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _cache_get(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            del self._cache[key]
            return None
        return value

    def _cache_put(self, key: str, value: Any) -> None:
        if len(self._cache) >= _CACHE_MAX:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[: _CACHE_MAX // 2]:
                del self._cache[k]
        self._cache[key] = (time.monotonic() + _BGM_TTL, value)

    # ---------------------------------------------------------- 联网搜索
    async def web_search(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"error": "query 不能为空"}
        session = await self._open()
        params = {"q": query, "setlang": "zh-CN"}
        try:
            async with session.get(
                _BING_URL,
                params=params,
                headers={"User-Agent": _BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=aiohttp.ClientTimeout(total=self._cfg.ai_search_timeout),
            ) as resp:
                if resp.status >= 400:
                    return {"error": f"搜索 HTTP {resp.status}"}
                page = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            return {"error": f"搜索失败: {type(exc).__name__}"}

        results = parse_bing(page)
        if not results:
            return {"query": query, "results": [], "note": "没有解析到结果，换个关键词试试"}
        return {"query": query, "results": results}

    # ---------------------------------------------------------- 频道索引
    async def channel_search(self, keyword: str, channel: Any = None) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            return {"error": "keyword 不能为空"}
        wanted = [channel] if isinstance(channel, str) else list(self._searches)
        out: list[dict[str, Any]] = []
        for key in wanted:
            service = self._searches.get(key)
            if service is None:
                continue
            hits = await service.search(keyword, limit=5, strict=False)
            for hit in hits:
                post = hit.post
                out.append(
                    {
                        "channel": key,
                        "title": post.title_cn,
                        "score": post.score,
                        "episodes": post.episodes,
                        "air_date": post.air_date,
                        "tags": post.tags[:8],
                        "link": post.permalink(self._cfg.link_username),
                    }
                )
        return {"keyword": keyword, "found": len(out), "results": out}

    # ---------------------------------------------------------- 会话
    async def _open(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            proxy = self._cfg.proxy or None
            timeout = aiohttp.ClientTimeout(total=self._cfg.http_timeout)
            self._session = (
                aiohttp.ClientSession(timeout=timeout, proxy=proxy)
                if proxy
                else aiohttp.ClientSession(timeout=timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
