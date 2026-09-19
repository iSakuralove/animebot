"""搜索结果的展示层。渲染和查询分开，才能单独测渲染。

三条硬约束（都来自 Telegram，猜错要重写）：

1. **正文里的文字超链接只能开网址，不能触发回调。** 所以「点标题就地开详情」
   必须用 inline 按钮，标题在正文里就是纯文本 —— 不再做成跳原帖的超链接，
   那会和详情按钮功能重复（用户点标题跳原帖 vs 点数字看详情，两条路殊途同归）。

2. **callback_data 上限 64 字节**，网盘 URL 动辄 200+ 字符，绝不进 callback。
   真实直链走正文里的文字超链接（不占配额）和详情里的 URL 按钮（也不占配额）。

3. **一条纯文本消息不能被 edit 成带图消息。** 但 link preview 能显示图片：详情
   用 LinkPreviewOptions 指向原帖，Telegram 自动抓帖子的头图渲染在消息上方。
   这样「就地把列表改成带图详情」不需要 file_id（我们本来也没有）。

真实直链 vs 跳转表格的判定在 domain.links（按 URL 主机判，不信标签），
这里只负责把分好的组摆成按钮和文字。
"""

from __future__ import annotations

import html

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..domain.post import Post
from .callbacks import NOOP, QueryStore, encode_page
from .highlight import highlight, query_terms
from .service import SearchHit, SearchPage

_SUMMARY_CAP = 400


def _esc(s: str) -> str:
    return html.escape(s or "")


def _meta_bits(p: Post) -> list[str]:
    score = f"{p.score:.1f}分" if p.score is not None else "—"
    bits = [score]
    if p.episodes:
        bits.append(f"{_esc(p.episodes)}话")
    if p.air_date:
        bits.append(_esc(p.air_date))
    return bits


def _direct_link_html(p: Post) -> str:
    """真实直链拼成 ' · 百度网盘 · OneDrive' 的文字超链接串。

    只放 direct（真实网盘），跳转类（汇总表格/节点）不进列表 —— 那是全频道公用的
    东西，摆在每一行只是噪音。用户要的是「这部番自己的下载链接」。
    """
    parts = [
        f'<a href="{_esc(url)}">{_esc(label)}</a>'
        for _kind, label, url in p.direct_links
    ]
    return " · " + " · ".join(parts) if parts else ""


def format_hit_line(idx: int, hit: SearchHit, settings: Settings, terms: list[str]) -> str:
    """一条结果两行：序号+标题（超链接跳原帖，命中词高亮），然后元信息 + 真实直链。

    标题是超链接（点击跳到频道原帖），高亮的 <b> 嵌在 <a> 里 —— Telegram 的
    HTML 允许这种嵌套。数字按钮走另一条路（就地展开 bot 详情），两者目的不同：
    标题跳出去看原帖，数字留在会话里看结构化详情。
    """
    title = highlight(hit.post.title_cn, terms)
    link = _esc(hit.post.permalink(settings.link_username))
    meta = " · ".join(_meta_bits(hit.post))
    return f'{idx}. <a href="{link}">{title}</a>\n   <i>{meta}</i>{_direct_link_html(hit.post)}'


def render_page(page: SearchPage, settings: Settings) -> str:
    if page.is_empty:
        return (
            f"没找到 <b>{_esc(page.query)}</b>\n\n"
            "试试：换更短的关键词、只打前几个字，或者用标签搜 "
            "<code>#奇幻 #异世界</code>"
        )

    terms = query_terms(page.query)
    last = page.first_index + len(page.hits) - 1
    head = (
        f"🔍 <b>{_esc(page.query)}</b> · 共 {page.total} 条"
        f" · 第 {page.page + 1}/{page.pages} 页（{page.first_index}-{last}）"
    )
    body = "\n\n".join(
        format_hit_line(page.first_index + i, h, settings, terms)
        for i, h in enumerate(page.hits)
    )
    if page.fallback:
        # 标题零命中、回退了全文。挑明这批是「相关内容」而非标题命中，
        # 否则用户以为真有部叫「青春」的番。
        head = f"没匹配到标题，以下是“{_esc(page.query)}”相关内容：\n\n{head}"
    return f"{head}\n\n{body}"


def page_keyboard(page: SearchPage, store: QueryStore) -> InlineKeyboardMarkup | None:
    """只有翻页行。

    不再有序号按钮 —— 每条结果的标题本身就是跳原帖的超链接，序号按钮再开一个
    "无图详情" 是同一件事的第二条路，纯属重复。单页结果没有翻页行，返回 None。
    """
    if page.is_empty or page.pages <= 1:
        return None

    kb = InlineKeyboardBuilder()
    kb.row(*_nav_row(page, store))
    return kb.as_markup()


def _nav_row(page: SearchPage, store: QueryStore) -> list[InlineKeyboardButton]:
    """⏮ ◀ 3/72 ▶ ⏭

    到边界的按钮**保留但变成占位**，不移除。移除会让按钮位置在翻页时左右跳动，
    用户瞄准「下一页」结果点到了别的东西 —— 这是最烦人的一类交互 bug。
    """
    def nav(label: str, target: int, enabled: bool) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label if enabled else "·",
            callback_data=(
                encode_page(target, page.query, store, title_only=page.title_only)
                if enabled else NOOP
            ),
        )

    return [
        nav("⏮", 0, page.has_prev),
        nav("◀", page.page - 1, page.has_prev),
        InlineKeyboardButton(text=f"{page.page + 1}/{page.pages}", callback_data=NOOP),
        nav("▶", page.page + 1, page.has_next),
        nav("⏭", page.pages - 1, page.has_next),
    ]


# ---------------------------------------------------------------- 详情

_STAFF_LABELS = {
    "director": "导演", "script": "脚本", "original": "原作",
    "storyboard": "分镜", "studio": "制作", "music": "音乐",
}


def render_detail(post: Post, terms: list[str] | None = None) -> str:
    """详情正文。图片不在文字里 —— 由 detail_preview() 的 link preview 渲染在上方。

    不再收 settings：标题和原帖链接都移到了按钮上（detail_keyboard），正文里
    没有任何东西依赖频道 username。
    """
    terms = terms or []
    # 标题整行已经是 <b>，命中词再套 <b> 是 bold-on-bold 看不出来 —— 改用下划线，
    # 在加粗标题里仍然醒目。正文（非加粗）里继续用默认 <b> 高亮。
    lines = [f"<b>{highlight(post.title_cn, terms, tag='u')}</b>"]
    if post.title_en:
        lines.append(f"<i>{highlight(post.title_en, terms, tag='u')}</i>")

    meta: list[str] = []
    if post.score is not None:
        meta.append(f"评分 <b>{post.score:.1f}</b> {_esc(post.score_text)}")
    if post.episodes:
        meta.append(f"{_esc(post.episodes)} 话")
    if post.air_date:
        weekday = f" {_esc(post.air_weekday)}" if post.air_weekday else ""
        meta.append(_esc(post.air_date) + weekday)
    if meta:
        lines.append("\n" + " · ".join(meta))

    staff = [
        f"{_STAFF_LABELS[k]}: {_esc(v)}"
        for k, v in post.staff.items()
        if k in _STAFF_LABELS
    ]
    if staff:
        lines.append("\n" + "\n".join(staff))

    if post.summary:
        summary = post.summary
        if len(summary) > _SUMMARY_CAP:
            summary = summary[:_SUMMARY_CAP] + "…"
        lines.append(f"\n{highlight(summary, terms)}")

    if post.tags:
        lines.append("\n" + " ".join(f"#{_esc(t)}" for t in post.tags))
    if post.passwords:
        lines.append(f"\n🔑 解压: <code>{_esc(post.passwords[0])}</code>")
    return "\n".join(lines)


def detail_preview(post: Post, settings: Settings) -> LinkPreviewOptions:
    """让详情消息在文字上方显示频道帖的头图。

    指向原帖 permalink，Telegram 抓它的头图渲染。public 频道有效；internal 模式
    （t.me/c/ 私有链接）预览不出图，但消息本身正常 —— 优雅降级，不额外判分支。
    """
    return LinkPreviewOptions(
        url=post.permalink(settings.link_username),
        prefer_large_media=True,
        show_above_text=True,
    )


def detail_keyboard(
    post: Post, settings: Settings, back: str | None = None
) -> InlineKeyboardMarkup:
    """详情按钮：真实直链（主）→ 汇总表格/节点（次）→ 原帖 + 返回。

    URL 按钮不占 callback_data 配额，所以网盘直链放这里最合适。
    back 是返回按钮的 callback_data（回到来源页），列表点进来时给，
    /check 直接展示时不给（没有来源页）。
    """
    kb = InlineKeyboardBuilder()

    row: list[InlineKeyboardButton] = []
    for _kind, label, url in post.direct_links:
        row.append(InlineKeyboardButton(text=f"⬇️ {label}", url=url))
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    # 跳转类：汇总表格 / OD 节点，次级，一行铺开
    idx = [
        InlineKeyboardButton(text=label, url=url)
        for _kind, label, url in post.index_links
    ]
    for i in range(0, len(idx), 2):
        kb.row(*idx[i : i + 2])

    tail = [InlineKeyboardButton(text="📺 原帖", url=post.permalink(settings.link_username))]
    if back is not None:
        tail.append(InlineKeyboardButton(text="🔙 返回", callback_data=back))
    kb.row(*tail)
    return kb.as_markup()
