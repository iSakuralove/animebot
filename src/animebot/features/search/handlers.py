"""搜索指令。业务逻辑在 SearchService，渲染在 presenter，这里只做编排。"""

from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from ..._util import command_arg
from ...bot.help import render_command_list
from ...bot.ui import reply, safe_edit
from ...config import Settings
from ...core.errors import NotFound, UsageError
from ...core.registry import CommandRegistry, command
from ...observability.context import bind
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from ...search.callbacks import ANIME_PAGES, QueryStore
from ...search.presenter import (
    detail_keyboard,
    detail_preview,
    manga_switch_button,
    page_keyboard,
    render_detail,
    render_page,
)
from ...search.service import SearchPage, SearchService
from ...storage.repo import PostRepo

log = get_logger("animebot.search")

# 列表消息禁用链接预览：正文里有多条网盘直链，不禁的话 Telegram 会抓第一条
# （通常是百度）弹一个大预览卡，把列表挤下去。详情反过来 —— 特意开预览显示头图。
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class _MentionsMe(Filter):
    """群消息的第一个词是 @本 bot。

    Telegram 文档里「有保证投递」的只有三类：`/cmd@bot`、对 bot 消息的回复、
    以及菜单里选出的 ephemeral 指令。@提及不在那张清单上 —— 实测里 bot 是群
    管理员时能收到（管理员本来就收全部消息），不是管理员时不保证。

    所以这条入口是**锦上添花**：主路径是 `/s@bot` 和「回复 bot」，它们有文档
    保证。`bot.me()` 由 aiogram 缓存，热路径没有网络往返。
    """

    async def __call__(self, message: Message, bot: Bot) -> bool:
        text = (message.text or "").lstrip()
        if not text.startswith("@"):
            return False
        me = await bot.me()
        username = me.username
        if not username:
            return False
        head = text.split(maxsplit=1)[0]
        return head[1:].casefold() == username.casefold()


class _RepliesToMe(Filter):
    """群消息是对 bot 自己某条消息的回复。

    同样属于「明确针对 bot」的一类，隐私模式下也能收到。给了用户一条不打扰
    别人的路径：@bot 看到能干什么之后，直接回复那条消息打关键词就行。
    """

    async def __call__(self, message: Message, bot: Bot) -> bool:
        replied = message.reply_to_message
        return bool(
            replied
            and replied.from_user
            and replied.from_user.id == bot.id
        )


def _keyboard(
    page: SearchPage, query_store: QueryStore, settings: Settings
) -> InlineKeyboardMarkup | None:
    """结果键盘：manga 模块加载了才有「📚 漫画」切换按钮，否则点了没人接。"""
    switch = (
        manga_switch_button(page, query_store)
        if "manga" in settings.features else None
    )
    return page_keyboard(page, query_store, ANIME_PAGES, switch=switch)


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
    bind(feature="search")   # 纯文本没有指令名，归属得自己报
    await _reply_search(message, query, search, settings, query_store, strict=False)


async def on_mention(
    message: Message,
    bot: Bot,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
    registry: CommandRegistry,
    is_admin: bool = False,
) -> None:
    """群里的 `@bot 关键词` = 宽松搜索，和私聊纯文本同一条路。

    不带关键词时就是「这个 bot 能干什么」：列出指令，并给出群里最省事的用法 ——
    正好接住「@bot 后看得到指令，再输入关键词」这个交互。

    投递不保证（见 `_MentionsMe`），所以提示里给的第一选择是菜单里的 `/s`：
    菜单项标了 ephemeral，选出来既有保证投递、又不会打扰别人。
    """
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    bind(feature="search", command="mention", scope="anime", mode="lenient")
    if not query:
        me = await bot.me()
        name = f"@{me.username}" if me.username else "我"
        hint = (
            f"\n\n群里搜索：点输入框左边的 <b>/</b> 选 <code>/s</code>"
            f"（最稳，不会打扰别人）；也可以直接发 <code>{html.escape(name)} 关键词</code>"
            "，或者回复这条消息打关键词。"
        )
        bind(landing=True)
        await reply(
            message,
            render_command_list(registry, is_admin=is_admin) + hint,
        )
        return
    await _reply_search(message, query, search, settings, query_store, strict=False)


async def on_reply_to_me(
    message: Message,
    search: SearchService,
    settings: Settings,
    query_store: QueryStore,
) -> None:
    """群里回复 bot 的消息 = 搜索。

    和 @bot 关键词 等价，只是触发方式不同：`@bot` 看指令，然后直接回复那条
    消息打关键词。回复同样属于「明确针对 bot」，隐私模式不影响投递。
    """
    query = (message.text or "").strip()
    if not query or query.startswith("/"):   # 指令由 Command 过滤器接
        return
    bind(feature="search", command="reply", scope="anime", mode="lenient")
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
    """三个入口的共同主体。差别只在查询词来源和 strict，逻辑一份。"""
    bind(scope="anime", query=query[:80], mode="strict" if strict else "lenient")
    page = await search.search_page(query, strict=strict)
    METRICS.incr("search.query", found=not page.is_empty)
    METRICS.observe("search.results", page.total)
    bind(
        result_count=page.total,
        reason=page.hits[0].reason if page.hits else "none",
    )

    await reply(
        message,
        render_page(page, settings),
        reply_markup=_keyboard(page, query_store, settings),
        link_preview_options=_NO_PREVIEW,
    )


@command(
    "check",
    desc="判断某部番发过没有",
    usage="/check 无职英雄",
    rate=(6, 20),
    admin_only=True,
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

    bind(scope="anime", query=query[:80])
    hit = await search.check(query)
    METRICS.incr("check.query", found=hit is not None)
    bind(result_count=int(hit is not None))
    if hit is None:
        await reply(message, f"❌ <b>{html.escape(query)}</b> 没有发过")
        return
    p = hit.post
    await reply(
        message,
        render_detail(p),
        reply_markup=detail_keyboard(p, settings),
        link_preview_options=detail_preview(p, settings),
    )


@command(
    "tags", desc="查看标签列表", usage="/tags 或 /tags 引索", rate=(3, 20), admin_only=True
)
async def cmd_tags(message: Message, repo: PostRepo, settings: Settings) -> None:
    kind = "index" if command_arg(message) in ("引索", "index") else "tag"
    cloud = await repo.tag_cloud(kind, limit=40, channel_id=settings.channel_id)
    if not cloud:
        raise NotFound("标签")
    title = "引索" if kind == "index" else "标签"
    body = "  ".join(f"#{html.escape(t)}<code>({n})</code>" for t, n in cloud)
    await reply(message, f"<b>{title}</b>（共 {len(cloud)} 个）\n\n{body}")


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
    bind(feature="search", scope="anime")
    ref = ANIME_PAGES.decode(callback.data or "", query_store)
    if ref is None:
        # token 被 LRU 淘汰了。给明确提示而不是静默失败 —— 用户重搜一次就恢复。
        await callback.answer("搜索已过期，请重新搜索", show_alert=True)
        METRICS.incr("search.page_expired")
        bind(expired=True)
        return

    # 按原模式重查：严谨/宽松结果集不同，翻页不能换模式，否则串位。
    page = await search.search_page(
        ref.query, page=ref.page, strict=ref.strict
    )
    METRICS.incr("search.page_turn")
    bind(query=ref.query[:80], page=ref.page, result_count=page.total)
    await safe_edit(
        callback,
        render_page(page, settings),
        reply_markup=_keyboard(page, query_store, settings),
        link_preview_options=_NO_PREVIEW,
    )


async def on_noop(callback: CallbackQuery) -> None:
    """页码显示和到边界的占位按钮。必须 answer，否则客户端一直转圈。"""
    bind(feature="search")
    await callback.answer()


def register_callbacks(router: Router) -> None:
    """回调没有 @command 元数据，单独挂。

    只剩翻页（s:/t:）和占位（x）。序号详情按钮已删 —— 标题超链接直接跳原帖，
    不再需要 bot 自己渲染一份无图详情。
    """
    router.callback_query.register(on_page, F.data.func(ANIME_PAGES.owns))
    router.callback_query.register(on_noop, F.data == ANIME_PAGES.noop)


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


def register_mention_search(router: Router) -> None:
    """群里两条「明确针对 bot」的入口，都没有 @command 元数据。

    - `@bot 关键词` —— 直接搜；不带关键词就列指令。
    - 回复 bot 的任意消息 —— 当作关键词搜。

    只在群/超级群注册。群里不注册「所有纯文本」，否则每句闲聊都会被当搜索。
    两条入口都靠 Telegram 的隐私模式规则保证能收到：@提及和回复都属于
    「明确针对 bot」的消息，不需要关隐私、也不要求 bot 是管理员。
    """
    group = {"group", "supergroup"}
    router.message.register(
        on_mention,
        F.chat.type.in_(group),
        F.text,
        _MentionsMe(),
    )
    router.message.register(
        on_reply_to_me,
        F.chat.type.in_(group),
        F.text & ~F.text.startswith("/"),
        _RepliesToMe(),
    )
