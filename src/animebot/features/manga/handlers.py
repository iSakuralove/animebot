"""漫画指令 /c。

和动漫的搜索同一套检索、翻页、高亮，只换了两样：频道作用域（`manga_search`
是绑在漫画频道上的 SearchService）和结果行的元信息（presenter）。

/c 走宽松模式：标题命中优先，零命中再 fuzzy 纠错。动漫那边有「纯文本宽松 +
/s 严谨」两个入口；漫画只有 /c 一个入口，入口唯一时它必须能容错 —— 但纠错
出来的结果会在列表头上写明「名字相近」，不冒充命中。
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from ..._util import command_arg
from ...bot.ui import safe_edit
from ...config import Settings
from ...core.registry import command
from ...observability.context import bind
from ...observability.metrics import METRICS
from ...search.callbacks import MANGA_PAGES, QueryStore
from ...search.presenter import anime_switch_button, page_keyboard
from ...search.service import SearchPage, SearchService
from ...storage.repo import PostRepo
from .presenter import landing_keyboard, render_landing, render_page

# 列表里一条结果可能带三四个链接，不禁预览的话 Telegram 抓第一个弹大卡片。
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@command(
    "c",
    desc="搜漫画频道",
    usage="/c 进击的巨人",
    aliases=("manga", "comic"),
    rate=(6, 20),
    long_help=(
        "在漫画频道里按标题搜：中文名、别名、日文 / 英文原名都算。"
        "打错字会给出名字相近的，并在列表头上标明。\n\n"
        "例子:\n"
        "/c 进击的巨人\n"
        "/c #百合 #校园  （标签交集）\n"
        "/c #恋爱 青春   （标签 + 关键词）\n"
        "/c              （不带词：用法 + 热门标签按钮）"
    ),
)
async def cmd_manga(
    message: Message,
    manga_search: SearchService,
    repo: PostRepo,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    query = command_arg(message)
    bind(scope="manga")
    if not query:
        await _reply_landing(message, manga_search, repo, query_store)
        return

    bind(query=query[:80], mode="lenient")
    page = await manga_search.search_page(query, strict=False)
    _record(page)
    text, markup = await _present(page, manga_search, repo, settings, query_store)
    await message.reply(text, reply_markup=markup, link_preview_options=_NO_PREVIEW)


async def _reply_landing(
    message: Message, manga_search: SearchService, repo: PostRepo, store: QueryStore
) -> None:
    cid = manga_search.channel.channel_id
    total = await repo.count(channel_id=cid)
    files = await repo.file_count(channel_id=cid)
    cloud = await repo.tag_cloud("tag", limit=40, channel_id=cid)
    METRICS.incr("manga.landing")
    bind(landing=True, result_count=total)
    await message.reply(
        render_landing(total, files),
        reply_markup=landing_keyboard(cloud, total, store),
    )


def _record(page: SearchPage) -> None:
    """业务埋点 + 给这次请求的访问日志补字段（中间件那条 handler.done 会带上）。"""
    METRICS.incr("manga.query", found=not page.is_empty)
    METRICS.observe("manga.results", page.total)
    bind(
        result_count=page.total,
        page=page.page,
        reason=page.hits[0].reason if page.hits else "none",
    )


async def _present(
    page: SearchPage,
    manga_search: SearchService,
    repo: PostRepo,
    settings: Settings,
    store: QueryStore,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """一页结果 -> (正文, 键盘)。指令和翻页共用，排版只有一份。

    「🎬 动漫」切换按钮只在 search 模块加载了才给 —— 它的 callback 要由 search
    模块的 on_page 接住，对方不在时点了会一直转圈。
    """
    channel = manga_search.channel
    files = await repo.file_stats(channel.channel_id, [h.post.message_id for h in page.hits])
    text = render_page(page, files, channel.link_username(settings.link_mode))
    switch = (
        anime_switch_button(page, store)
        if "search" in settings.features else None
    )
    return text, page_keyboard(page, store, MANGA_PAGES, switch=switch)


async def on_page(
    callback: CallbackQuery,
    manga_search: SearchService,
    repo: PostRepo,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    """翻页，以及落地页的标签按钮（标签按钮就是第 0 页的 `#标签` 查询）。"""
    bind(feature="manga", scope="manga")
    ref = MANGA_PAGES.decode(callback.data or "", query_store)
    if ref is None:
        # token 被 LRU 淘汰了（进程重启也会）。重搜一次就恢复。
        await callback.answer("搜索已过期，请重新搜索", show_alert=True)
        METRICS.incr("manga.page_expired")
        bind(expired=True)
        return

    page = await manga_search.search_page(ref.query, page=ref.page, strict=ref.strict)
    METRICS.incr("manga.page_turn")
    bind(query=ref.query[:80], mode="strict" if ref.strict else "lenient")
    _record(page)
    text, markup = await _present(page, manga_search, repo, settings, query_store)
    await safe_edit(callback, text, reply_markup=markup, link_preview_options=_NO_PREVIEW)


async def on_noop(callback: CallbackQuery) -> None:
    """页码和到边界的占位按钮。必须 answer，否则客户端一直转圈。"""
    bind(feature="manga")
    await callback.answer()


def register_callbacks(router: Router) -> None:
    """只认漫画自己的前缀（c: / ct: / cx）。动漫的 s: / t: / x 由 search 模块接，
    两个模块互不依赖对方被加载。"""
    router.callback_query.register(on_page, F.data.func(MANGA_PAGES.owns))
    router.callback_query.register(on_noop, F.data == MANGA_PAGES.noop)
