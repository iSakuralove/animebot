"""搜索指令。业务逻辑在 SearchService，渲染在 presenter，这里只做编排。"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from ..._util import command_arg
from ...config import Settings
from ...core.errors import NotFound, UsageError
from ...core.registry import command
from ...observability.context import RequestContext, bind
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from ...search.callbacks import (
    NOOP,
    PREFIX_DETAIL,
    PREFIX_INLINE,
    PREFIX_TOKEN,
    QueryStore,
    decode_page,
)
from ...search.presenter import (
    detail_keyboard,
    page_keyboard,
    render_detail,
    render_page,
)
from ...search.service import SearchService
from ...storage.repo import PostRepo

log = get_logger("animebot.search")


@command(
    "search",
    desc="搜索番剧",
    usage="/search 无职英雄",
    aliases=("s", "find"),
    rate=(6, 20),
    long_help=(
        "支持多个关键词（空格分隔，命中越多排越前）、错别字容错、"
        "标签筛选。结果多于一页时下面会出现翻页按钮。\n\n"
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
    query_store: QueryStore,
) -> None:
    query = command_arg(message)
    if not query:
        raise UsageError("要搜什么？", "/search 无职英雄")

    bind(query=query[:80])
    page = await search.search_page(query)
    METRICS.incr("search.query", found=not page.is_empty)
    METRICS.observe("search.results", page.total)
    trace.bind(
        result_count=page.total,
        reason=page.hits[0].reason if page.hits else "none",
    )

    await message.reply(
        render_page(page, settings),
        reply_markup=page_keyboard(page, query_store),
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


async def on_page(
    callback: CallbackQuery,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    """翻页：原地编辑消息，不发新的。

    发新消息会让聊天记录被同一次搜索的十几个版本刷满。编辑是标准做法，
    代价是要处理 "message is not modified"。
    """
    ref = decode_page(callback.data or "", query_store)
    if ref is None:
        # token 被 LRU 淘汰了。给明确提示而不是静默失败 —— 用户重搜一次就恢复。
        await callback.answer("搜索已过期，请重新搜索", show_alert=True)
        METRICS.incr("search.page_expired")
        return

    page = await search.search_page(ref.query, page=ref.page)
    METRICS.incr("search.page_turn")
    bind(query=ref.query[:80], page=ref.page)

    if callback.message is None:
        await callback.answer()
        return

    try:
        await callback.message.edit_text(
            render_page(page, settings),
            reply_markup=page_keyboard(page, query_store),
        )
    except TelegramBadRequest as exc:
        # 内容完全没变时 Telegram 报这个。不是错误，用户重复点了同一页。
        if "not modified" not in str(exc).lower():
            raise
    await callback.answer()


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
        # 详情作为新消息发出，保留原来的结果列表 —— 用户看完详情还要回去翻页
        await callback.message.answer(
            render_detail(post, settings),
            reply_markup=detail_keyboard(post, settings),
        )
    await callback.answer()


async def on_noop(callback: CallbackQuery) -> None:
    """页码显示和到边界的占位按钮。必须 answer，否则客户端一直转圈。"""
    await callback.answer()


def register_callbacks(router: Router) -> None:
    """回调没有 @command 元数据，单独挂。"""
    router.callback_query.register(on_detail, F.data.startswith(f"{PREFIX_DETAIL}:"))
    router.callback_query.register(
        on_page,
        F.data.startswith(f"{PREFIX_INLINE}:") | F.data.startswith(f"{PREFIX_TOKEN}:"),
    )
    router.callback_query.register(on_noop, F.data == NOOP)
