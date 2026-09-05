"""搜索结果的展示层。渲染和查询分开，才能单独测渲染。

callback_data 只放 'p:<message_id>'：Telegram 上限 64 字节，
往里塞网盘 URL 一定会爆（真实数据里的 sharepoint 链接有 200+ 字符）。
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..domain.post import Post
from .service import SearchHit

CB_DETAIL = "p"      # p:<message_id>
CB_PAGE = "pg"       # pg:<offset>:<query hash>

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
    ("sheet", "表格"),
    ("official", "官网"),
)


def _esc(s: str) -> str:
    return html.escape(s or "")


def format_hit_line(idx: int, hit: SearchHit, settings: Settings) -> str:
    p = hit.post
    score = f"{p.score:.1f}" if p.score is not None else "—"
    bits = [f"{score}分"]
    if p.episodes:
        bits.append(f"{_esc(p.episodes)}话")
    if p.air_date:
        bits.append(_esc(p.air_date))
    link = p.permalink(settings.link_username)
    return (
        f"{idx}. <a href=\"{link}\">{_esc(p.title_cn)}</a>\n"
        f"   <i>{' · '.join(bits)}</i>"
    )


def render_results(
    query: str,
    hits: list[SearchHit],
    settings: Settings,
    *,
    total: int | None = None,
) -> str:
    if not hits:
        return (
            f"没找到 <b>{_esc(query)}</b>\n\n"
            "试试：换更短的关键词、只打前几个字，或者用标签搜 "
            "<code>#奇幻 #异世界</code>"
        )
    head = f"<b>{_esc(query)}</b> — 找到 {total if total is not None else len(hits)} 条"
    body = "\n".join(format_hit_line(i, h, settings) for i, h in enumerate(hits, 1))
    return f"{head}\n\n{body}"


def results_keyboard(hits: list[SearchHit]) -> InlineKeyboardMarkup | None:
    """每条结果一个按钮，点了出详情。callback_data 只放 message_id。"""
    if not hits:
        return None
    kb = InlineKeyboardBuilder()
    for i, h in enumerate(hits, 1):
        kb.button(text=str(i), callback_data=f"{CB_DETAIL}:{h.post.message_id}")
    kb.adjust(8)
    return kb.as_markup()


def render_detail(post: Post, settings: Settings) -> str:
    lines = [f"<b>{_esc(post.title_cn)}</b>"]
    if post.title_en:
        lines.append(f"<i>{_esc(post.title_en)}</i>")

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
        lines.append(f"\n{_esc(text)}")

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
