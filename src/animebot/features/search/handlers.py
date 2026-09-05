"""搜索指令。业务逻辑在 SearchService，渲染在 presenter，这里只做编排。"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from ..._util import command_arg
from ...config import Settings
from ...core.errors import NotFound, UsageError
from ...core.registry import command
from ...observability.context import RequestContext, bind
from ...observability.metrics import METRICS
from ...search.presenter import (
    CB_DETAIL,
    detail_keyboard,
    render_detail,
    render_results,
    results_keyboard,
)
from ...search.service import SearchService
from ...storage.repo import PostRepo


@command(
    "search",
    desc="搜索番剧",
    usage="/search 无职英雄",
    aliases=("s", "find"),
    rate=(6, 20),
    long_help=(
        "支持多个关键词（空格分隔，命中越多排越前）、错别字容错、"
        "标签筛选。\n\n"
        "例子:\n"
        "/search 无职英雄\n"
        "/search 咒术回站      （打错字也能搜到）\n"
        "/search #奇幻 #异世界  （标签交集）\n"
        "/search #百合 学园     （标签 + 关键词）"
    ),
)
async def cmd_search(
    message: Message,
    search: SearchService,
    settings: Settings,
    trace: RequestContext,
) -> None:
    query = command_arg(message)
    if not query:
        raise UsageError("要搜什么？", "/search 无职英雄")

    bind(query=query[:80])
    hits = await search.search(query)
    METRICS.incr("search.query", found=bool(hits))
    METRICS.observe("search.results", len(hits))
    trace.bind(result_count=len(hits), reason=hits[0].reason if hits else "none")

    await message.reply(
        render_results(query, hits, settings),
        reply_markup=results_keyboard(hits),
    )


@command(
    "check",
    desc="判断某部番发过没有",
    usage="/check 无职英雄",
    rate=(6, 20),
    long_help="只回答发过 / 没发过，发过就给原帖链接。",
)
async def cmd_check(
    message: Message,
    search: SearchService,
    settings: Settings,
) -> None:
    query = command_arg(message)
    if not query:
        raise UsageError("要查什么？", "/check 无职英雄")

    bind(query=query[:80])
    hit = await search.check(query)
    METRICS.incr("check.query", found=hit is not None)
    if hit is None:
        await message.reply(f"❌ <b>{html.escape(query)}</b> 没有发过")
        return
    p = hit.post
    await message.reply(
        f"✅ 发过 — <a href=\"{p.permalink(settings.link_username)}\">"
        f"{html.escape(p.title_cn)}</a>",
        reply_markup=detail_keyboard(p, settings),
    )


@command("tags", desc="查看标签列表", usage="/tags 或 /tags 引索", rate=(3, 20))
async def cmd_tags(message: Message, repo: PostRepo) -> None:
    kind = "index" if command_arg(message) in ("引索", "index") else "tag"
    cloud = await repo.tag_cloud(kind, limit=40)
    if not cloud:
        raise NotFound("标签")
    title = "引索" if kind == "index" else "标签"
    body = "  ".join(f"#{html.escape(t)}<code>({n})</code>" for t, n in cloud)
    await message.reply(f"<b>{title}</b>（共 {len(cloud)} 个）\n\n{body}")


async def on_detail(
    callback: CallbackQuery,
    repo: PostRepo,
    settings: Settings,
) -> None:
    """结果列表里点序号 -> 出详情。callback_data 里只有 message_id。"""
    raw = (callback.data or "").split(":", 1)
    if len(raw) != 2 or not raw[1].isdigit():
        await callback.answer("按钮已失效", show_alert=True)
        return

    post = await repo.get(settings.channel_id, int(raw[1]))
    if post is None:
        await callback.answer("这条帖子不在索引里了", show_alert=True)
        return

    METRICS.incr("search.detail_open")
    if callback.message is not None:
        await callback.message.answer(
            render_detail(post, settings),
            reply_markup=detail_keyboard(post, settings),
        )
    await callback.answer()


def register_callbacks(router: Router) -> None:
    """回调没有 @command 元数据，单独挂。"""
    router.callback_query.register(on_detail, F.data.startswith(f"{CB_DETAIL}:"))
