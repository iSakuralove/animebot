"""搜索指令。业务逻辑在 SearchService，渲染在 presenter，这里只做编排。"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from ..._util import command_arg
from ...config import Settings
from ...core.errors import NotFound, UsageError
from ...core.registry import command
from ...observability.context import RequestContext, bind
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from ...search.callbacks import (
    NOOP,
    PREFIX_INLINE,
    PREFIX_TOKEN,
    QueryStore,
    decode_page,
)
from ...search.presenter import (
    detail_keyboard,
    detail_preview,
    page_keyboard,
    render_detail,
    render_page,
)
from ...search.service import SearchService
from ...storage.repo import PostRepo

log = get_logger("animebot.search")

# 列表消息禁用链接预览：正文里有多条网盘直链，不禁的话 Telegram 会抓第一条
# （通常是百度）弹一个大预览卡，把列表挤下去。详情反过来 —— 特意开预览显示头图。
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@command(
    "s",
    desc="搜索番剧",
    usage="/s 无职英雄",
    aliases=("search", "find"),
    rate=(6, 20),
    long_help=(
        "支持多个关键词（空格分隔，命中越多排越前）、错别字容错、"
        "标签筛选。结果多于一页时下面会出现翻页按钮。\n\n"
        "例子:\n"
        "/s 无职英雄\n"
        "/s 咒术回站      （打错字也能搜到）\n"
        "/s #奇幻 #异世界  （标签交集）\n"
        "/s #百合 学园     （标签 + 关键词）"
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
        raise UsageError("要搜什么？", "/s 无职英雄")

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
        link_preview_options=_NO_PREVIEW,
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
        render_detail(p),
        reply_markup=detail_keyboard(p, settings),
        link_preview_options=detail_preview(p, settings),
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


async def _safe_edit(callback: CallbackQuery, text: str, **kw: object) -> None:
    """原地编辑并 answer。吞掉 "not modified"（重复点同一按钮），其余照抛。

    编辑而不是发新消息：翻页和进出详情都在同一条消息上完成，聊天记录不会被
    同一次搜索的十几个版本刷屏 —— 这是用户明确要的「就地修改」。
    """
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, **kw)  # type: ignore[arg-type]
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
    await callback.answer()


async def on_page(
    callback: CallbackQuery,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    """翻页，以及从详情点「返回」（返回按钮就是一个 encode_page）。

    回到列表态必须禁用链接预览 —— 从带图详情返回时，不显式关掉的话上一条
    详情的头图预览会残留。
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
    await _safe_edit(
        callback,
        render_page(page, settings),
        reply_markup=page_keyboard(page, query_store),
        link_preview_options=_NO_PREVIEW,
    )


async def on_noop(callback: CallbackQuery) -> None:
    """页码显示和到边界的占位按钮。必须 answer，否则客户端一直转圈。"""
    await callback.answer()


def register_callbacks(router: Router) -> None:
    """回调没有 @command 元数据，单独挂。

    只剩翻页（s:/t:）和占位（x）。序号详情按钮已删 —— 标题超链接直接跳原帖，
    不再需要 bot 自己渲染一份无图详情。
    """
    router.callback_query.register(
        on_page,
        F.data.startswith(f"{PREFIX_INLINE}:") | F.data.startswith(f"{PREFIX_TOKEN}:"),
    )
    router.callback_query.register(on_noop, F.data == NOOP)
