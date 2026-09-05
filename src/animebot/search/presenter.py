"""搜索结果的展示层。渲染和查询分开，才能单独测渲染。

callback_data 有 64 **字节**上限（不是字符）。一个汉字 3 字节，所以：
  - 详情按钮只放 `p:<message_id>`
  - 分页按钮的查询词能内联就内联，超长退到 token（见 callbacks.py）

往里塞网盘 URL 一定会爆 —— 真实数据里的 sharepoint 链接有 200+ 字符。
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..domain.post import Post
from .callbacks import NOOP, PREFIX_DETAIL, QueryStore, encode_page
from .highlight import highlight, query_terms
from .service import SearchHit, SearchPage

CB_DETAIL = PREFIX_DETAIL

# 网盘类型 -> 展示名。顺序即按钮顺序。
_LINK_LABELS: tuple[tuple[str, str], ...] = (
    ("baidu", "百度网盘"),
    ("onedrive", "OneDrive"),
    ("gdrive", "谷歌网盘"),
    ("aliyun", "阿里网盘"),
    ("quark", "夸克网盘"),
    ("od_node", "OD节点"),
    ("cdn_node", "CDN节点"),
    ("raw_disk", "原盘"),
    ("official", "官网"),
    ("sheet", "表格"),
)

# 结果列表里每行的序号按钮，一行放 8 个
_INDEX_ROW = 8


def _esc(s: str) -> str:
    return html.escape(s or "")


def format_hit_line(
    idx: int, hit: SearchHit, settings: Settings, terms: list[str]
) -> str:
    """一条结果两行：序号+标题（带链接、高亮），然后是元信息。"""
    p = hit.post
    score = f"{p.score:.1f}" if p.score is not None else "—"
    bits = [f"{score}分"]
    if p.episodes:
        bits.append(f"{_esc(p.episodes)}话")
    if p.air_date:
        bits.append(_esc(p.air_date))
    link = p.permalink(settings.link_username)
    title = highlight(p.title_cn, terms)
    return f"{idx}. <a href=\"{link}\">{title}</a>\n   <i>{' · '.join(bits)}</i>"


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
        f"<b>{_esc(page.query)}</b> — 共 {page.total} 条"
        f"（{page.first_index}-{last}，第 {page.page + 1}/{page.pages} 页）"
    )
    body = "\n".join(
        format_hit_line(page.first_index + i, h, settings, terms)
        for i, h in enumerate(page.hits)
    )
    return f"{head}\n\n{body}"


def page_keyboard(page: SearchPage, store: QueryStore) -> InlineKeyboardMarkup | None:
    """序号按钮 + 翻页行。

    序号用的是**全局序号**（第 2 页显示 9~16），和正文一致 —— 用户看到「9」
    就点「9」，不需要在心里做换算。
    """
    if page.is_empty:
        return None

    kb = InlineKeyboardBuilder()
    for i, h in enumerate(page.hits):
        kb.button(
            text=str(page.first_index + i),
            callback_data=f"{CB_DETAIL}:{h.post.message_id}",
        )
    kb.adjust(_INDEX_ROW)

    if page.pages > 1:
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
                encode_page(target, page.query, store) if enabled else NOOP
            ),
        )

    return [
        nav("⏮", 0, page.has_prev),
        nav("◀", page.page - 1, page.has_prev),
        # 页码本身不可点，但保留成按钮以固定行宽
        InlineKeyboardButton(text=f"{page.page + 1}/{page.pages}", callback_data=NOOP),
        nav("▶", page.page + 1, page.has_next),
        nav("⏭", page.pages - 1, page.has_next),
    ]


def render_detail(post: Post, settings: Settings, terms: list[str] | None = None) -> str:
    terms = terms or []
    lines = [f"<b>{highlight(post.title_cn, terms)}</b>"]
    if post.title_en:
        lines.append(f"<i>{highlight(post.title_en, terms)}</i>")

    meta: list[str] = []
    if post.score is not None:
        meta.append(f"评分 <b>{post.score:.1f}</b> {_esc(post.score_text)}")
    if post.episodes:
        meta.append(f"{_esc(post.episodes)} 话")
    if post.air_date:
        weekday = f" {_esc(post.air_weekday)}" if post.air_weekday else ""
        meta.append(_esc(post.air_date) + weekday)
    if meta:
        lines.append("\n" + "\n".join(meta))

    if post.staff:
        labels = {
            "director": "导演", "script": "脚本", "original": "原作",
            "storyboard": "分镜", "studio": "制作", "music": "音乐",
        }
        staff = [
            f"{labels.get(k, k)}: {_esc(v)}"
            for k, v in post.staff.items()
            if k in labels
        ]
        if staff:
            lines.append("\n" + "\n".join(staff))

    if post.summary:
        text = post.summary if len(post.summary) <= 400 else post.summary[:400] + "…"
        lines.append(f"\n{highlight(text, terms)}")

    if post.tags:
        lines.append("\n" + " ".join(f"#{_esc(t)}" for t in post.tags))
    if post.passwords:
        lines.append(f"\n解压: <code>{_esc(post.passwords[0])}</code>")

    lines.append(f"\n<a href=\"{post.permalink(settings.link_username)}\">→ 查看原帖</a>")
    return "\n".join(lines)


def detail_keyboard(post: Post, settings: Settings) -> InlineKeyboardMarkup:
    """原帖链接 + 各网盘直链。URL 按钮不占 callback_data 配额。"""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📺 原帖", url=post.permalink(settings.link_username)
        )
    )
    row: list[InlineKeyboardButton] = []
    for key, label in _LINK_LABELS:
        if url := post.links.get(key):
            row.append(InlineKeyboardButton(text=label, url=url))
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    return kb.as_markup()
