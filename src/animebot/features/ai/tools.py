"""给模型用的工具。

三个，都是「模型自己查不到、但用户会问」的东西：

- `bangumi_search` —— 番剧评分 / 排名 / 放送日期。模型记忆里的评分会过期。
- `web_search` —— 联网搜。网关会把服务端工具（web_search_20250305）静默丢弃，
  所以只能我们搜完把结果回灌。
- `channel_search` —— 查我们自己的索引。这是这个 bot 独有的：用户问
  「频道里发过没」，模型知道答案只能靠这个。

工具返回一律是 JSON 字符串（模型对结构化输入更稳），出错时返回 `{"error": ...}`
而不是抛异常 —— 一次工具失败不该让整轮对话崩掉。
"""

from __future__ import annotations

import base64
import html
import json
import re
import urllib.parse
from typing import Any

import aiohttp

from ...config import Settings
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from ...search.service import SearchService

log = get_logger("animebot.ai.tools")

_BANGUMI_URL = "https://api.bgm.tv/v0/search/subjects"
_BGM_TYPES = {"anime": 2, "book": 1, "music": 3, "game": 4}
_BGM_UA = "animebot/0.1 (https://github.com/iSakuralove/animebot)"

_BING_URL = "https://www.bing.com/search"
_BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BLOCK = re.compile(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', re.S)
_H2 = re.compile(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)
_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")


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

    # ---------------------------------------------------------- 声明
    @staticmethod
    def specs() -> list[dict[str, Any]]:
        return [
            {
                "name": "bangumi_search",
                "description": (
                    "在 Bangumi（bgm.tv）搜索番剧 / 书籍 / 音乐 / 游戏，返回中文名、"
                    "原名、评分、排名、评分人数、放送日期。问评分、排名、放送时间、"
                    "制作信息时用它，不要凭记忆回答。"
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
                "name": "web_search",
                "description": (
                    "联网搜索网页，返回标题、链接和摘要。用于查询时效性信息"
                    "（新番、新闻、事件）或 Bangumi 查不到的东西。"
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
        METRICS.incr("ai.tool", tool=name, outcome="ok")
        try:
            if name == "bangumi_search":
                data = await self.bangumi_search(
                    str(args.get("keyword", "")), str(args.get("type", "anime"))
                )
            elif name == "web_search":
                data = await self.web_search(str(args.get("query", "")))
            elif name == "channel_search":
                data = await self.channel_search(
                    str(args.get("keyword", "")), args.get("channel")
                )
            else:
                data = {"error": f"没有这个工具: {name}"}
        except Exception as exc:
            METRICS.incr("ai.tool", tool=name, outcome="error")
            log.warning("ai.tool_failed", tool=name, error=repr(exc))
            data = {"error": f"{type(exc).__name__}: {exc}"}
        return json.dumps(data, ensure_ascii=False)

    async def bangumi_search(self, keyword: str, kind: str = "anime") -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            return {"error": "keyword 不能为空"}
        subject_type = _BGM_TYPES.get(kind, 2)
        body = {"keyword": keyword, "filter": {"type": [subject_type]}}
        url = f"{_BANGUMI_URL}?limit=5"
        session = await self._open()
        try:
            async with session.post(
                url,
                json=body,
                headers={"User-Agent": _BGM_UA, "content-type": "application/json"},
            ) as resp:
                if resp.status >= 400:
                    return {"error": f"Bangumi HTTP {resp.status}"}
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            return {"error": f"Bangumi 访问失败: {type(exc).__name__}"}

        items = payload.get("data") or []
        results = []
        for item in items:
            rating = item.get("rating") or {}
            results.append(
                {
                    "name": item.get("name"),
                    "name_cn": item.get("name_cn"),
                    "score": rating.get("score"),
                    "rank": rating.get("rank"),
                    "votes": rating.get("total"),
                    "date": item.get("date"),
                    "platform": item.get("platform"),
                    "url": f"https://bgm.tv/subject/{item.get('id')}",
                }
            )
        return {"keyword": keyword, "total": payload.get("total", len(results)), "results": results}

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
