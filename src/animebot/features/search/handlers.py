"""搜索指令。业务逻辑在 SearchService，渲染在 presenter，这里只做编排。"""

from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from ..._util import command_arg
from ...config import Settings
from ...core.errors import NotFound, UsageError
from ...core.registry import command
from ...observability.context import bind
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
    desc="严谨搜索（精确匹配标题，不猜错字）",
    usage="/s 无职英雄",
    aliases=("search", "find"),
    rate=(6, 20),
    long_help=(
        "严谨模式：标题精确子串匹配，打错字不会纠正、也不返回相关内容 —— "
        "要么命中要么没有。想让打错字也能搜到，直接发文字（不加 /s）。\n\n"
        "例子:\n"
        "/s 无职英雄\n"
        "/s #奇幻 #异世界  （标签交集）\n"
        "/s #百合 学园     （标签 + 关键词）"
    ),
)
async def cmd_search(
    message: Message,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    query = command_arg(message)
    if not query:
        raise UsageError("要搜什么？", "/s 无职英雄")
    # /s = 严谨：标题子串精确匹配，不 fuzzy、不回退全文。没有就是没有。
    await _reply_search(message, query, search, settings, query_store, strict=True)


async def on_plain_text(
    message: Message,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    """私聊里的纯文本 = 宽松搜索。只在私聊注册（见 register_plain_search）。

    标题命中优先；零命中走 fuzzy 纠错（「咒术回站」→「咒术回战」），仍在标题域。
    这是默认、宽容的入口；要精确匹配就用 /s。
    """
    query = (message.text or "").strip()
    if not query:   # 过滤器已挡掉无文本消息，这里只是防御
        return
    await _reply_search(message, query, search, settings, query_store, strict=False)


async def _reply_search(
    message: Message,
    query: str,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
    *,
    strict: bool,
) -> None:
    """两个入口的共同主体。差别只在查询词来源和 strict，逻辑一份。"""
    bind(query=query[:80])
    page = await search.search_page(query, strict=strict)
    METRICS.incr("search.query", found=not page.is_empty)
    METRICS.observe("search.results", page.total)
    bind(
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
    if callback.message is None:
        await callback.answer()
        return

    # answer() 和 edit_text() 无先后依赖，并发发：走代理时每次往返 ~350ms，
    # 串行是 700ms，并发是 350ms —— 少一整个往返，且转圈立刻停。
    #
    # 必须 await 成 coroutine 再交给 gather：aiogram 的 .answer()/.edit_text()
    # 返回的是 TelegramMethod 对象（pydantic model，可 await 但不是 coroutine），
    # 直接塞进 gather 会触发 "unhashable type" —— gather 拿它当 key 而 model 无 __hash__。
    async def _answer() -> None:
        await callback.answer()

    async def _edit() -> None:
        await callback.message.edit_text(text, **kw)  # type: ignore[union-attr, arg-type]

    _, edit_exc = await asyncio.gather(_answer(), _edit(), return_exceptions=True)
    if isinstance(edit_exc, TelegramBadRequest):
        # 内容没变时 Telegram 报 not modified，是重复点击，吞掉。
        if "not modified" not in str(edit_exc).lower():
            raise edit_exc
    elif isinstance(edit_exc, BaseException):
        raise edit_exc


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

    # 按原模式重查：严谨/宽松结果集不同，翻页不能换模式，否则串位。
    page = await search.search_page(
        ref.query, page=ref.page, strict=ref.strict
    )
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


def register_plain_search(router: Router) -> None:
    """私聊纯文本 = 标题搜索。也没有 @command 元数据，单独挂。

    三个过滤器缺一不可：
    - `F.chat.type == "private"`：只在私聊。群里 catch 所有消息会把每句闲聊
      当搜索，是灾难 —— 群里搜索必须显式 /s。
    - `F.text`：挡掉图片/贴纸等无文本消息（magic_filter 对 None 返回 falsy）。
    - `~F.text.startswith("/")`：不抢指令。/s 等由 Command 过滤器先接走。
    """
    router.message.register(
        on_plain_text,
        F.chat.type == "private",
        F.text & ~F.text.startswith("/"),
    )
