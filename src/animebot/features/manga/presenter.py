"""漫画结果的展示。

和动漫列表同一个骨架：标题是跳原帖的超链接、命中词高亮、下面一行元信息、
多于一页才有翻页行。元信息换成挑漫画时真正看的东西 —— 评分、作者、卷数、
连载年份，以及「频道里有没有直接发整卷文件」。

点标题跳原帖而不是在 bot 里重渲染（ADR-0005）：原帖有封面、有排版，2024 年
之后的帖子资源都在帖子评论区，只有跳过去才拿得到。
"""

from __future__ import annotations

import html
import re

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...domain.files import PostFiles
from ...domain.post import Post, message_link
from ...search.callbacks import MANGA_PAGES, QueryStore
from ...search.highlight import highlight, query_terms
from ...search.service import SearchHit, SearchPage

_YEAR = re.compile(r"(?:19|20)\d{2}")
_LEADING_INT = re.compile(r"\d+")
_WHO_MAX = 16          # 合集的作画一栏能列七八个人，截断
_LANDING_TAGS = 12     # 落地页标签按钮：4 行 × 3 个


def _esc(s: str) -> str:
    return html.escape(s or "")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _year(text: str) -> str:
    mo = _YEAR.search(text or "")
    return mo.group(0) if mo else ""


def years(p: Post) -> str:
    """连载起止年：「2009–2021」。只有一头就写一头；都没有退到发售年（单行本常见）。"""
    start = _year(p.air_date)
    end = _year(p.extra.get("end_date", ""))
    if start and end and start != end:
        return f"{start}–{end}"
    return start or end or _year(p.extra.get("release_date", ""))


def volumes(p: Post) -> str:
    """「册数」的原文五花八门（20 / 14卷既刊 (2021-05-31) / 34+外传+LOST GIRLS…），
    只取打头的数字。没有册数就退到话数。细节在原帖里，列表只要一个量级。"""
    for raw, unit in ((p.extra.get("volumes", ""), "卷"), (p.episodes, "话")):
        if mo := _LEADING_INT.match(raw.strip()):
            return f"{mo.group(0)}{unit}"
    return ""


def author(p: Post) -> str:
    return _clip((p.staff.get("original") or p.staff.get("art") or "").strip(), _WHO_MAX)


def human_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f}GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f}MB"
    return f"{max(n, 0) / 1024:.0f}KB"


def _meta(p: Post) -> str:
    bits = [f"{p.score:.1f}分"] if p.score is not None else []
    bits += [b for b in (_esc(author(p)), volumes(p), years(p)) if b]
    return " · ".join(bits)


def _links(p: Post, files: PostFiles | None, username: str | None) -> list[str]:
    """整卷文件排第一（最直接能拿到的），然后是资料页 / 在线看，最后是网盘。"""
    out: list[str] = []
    if files is not None:
        url = message_link(p.channel_id, files.first_message_id, username)
        out.append(
            f'<a href="{_esc(url)}">📦{files.count}个文件 {human_size(files.total_size)}</a>'
        )
    out += [
        f'<a href="{_esc(url)}">{_esc(label)}</a>'
        for _kind, label, url in (*p.info_links, *p.direct_links)
    ]
    return out


def format_hit_line(
    idx: int,
    hit: SearchHit,
    terms: list[str],
    files: PostFiles | None,
    username: str | None,
) -> str:
    p = hit.post
    head = f'{idx}. <a href="{_esc(p.permalink(username))}">{highlight(p.title_cn, terms)}</a>'
    meta = _meta(p)
    parts = [f"<i>{meta}</i>"] if meta else []
    parts += _links(p, files, username)
    return f"{head}\n   {' · '.join(parts)}" if parts else head


def render_page(
    page: SearchPage, files: dict[int, PostFiles], username: str | None
) -> str:
    if page.is_empty:
        return (
            f"📚 漫画频道里没找到 <b>{_esc(page.query)}</b>\n\n"
            "试试：换更短的关键词、只打前几个字、换成日文 / 英文原名，"
            "或者用标签搜 <code>/c #百合</code>"
        )

    terms = query_terms(page.query)
    last = page.first_index + len(page.hits) - 1
    head = (
        f"📚 <b>{_esc(page.query)}</b> · 共 {page.total} 部"
        f" · 第 {page.page + 1}/{page.pages} 页（{page.first_index}-{last}）"
    )
    # 全是 fuzzy 纠错出来的 —— 说清楚这是「名字相近」，不是命中。
    if all(h.reason == "fuzzy" for h in page.hits):
        head += "\n<i>没有标题包含这个词，下面是名字相近的：</i>"
    body = "\n\n".join(
        format_hit_line(
            page.first_index + i, h, terms, files.get(h.post.message_id), username
        )
        for i, h in enumerate(page.hits)
    )
    return f"{head}\n\n{body}"


def render_landing(total: int, files: int) -> str:
    """只发 /c 不带词：用法 + 热门标签。比一句「要搜什么？」有用。"""
    if not total:
        return "📚 漫画频道还没有数据（管理员还没回填导出）。"
    file_line = f"；带 📦 的在频道里直接有整卷文件（共 {files} 个）" if files else ""
    return (
        f"📚 <b>漫画检索</b> · 共 {total} 部\n\n"
        "<code>/c 进击的巨人</code>  搜标题：中文名、别名、日文原名都算\n"
        "<code>/c #百合 #校园</code>  标签交集\n"
        "<code>/c #恋爱 青春</code>  标签 + 关键词\n\n"
        f"打错字也能找到名字相近的。点标题跳到频道原帖，资源在帖子评论区{file_line}。\n\n"
        "或者点个标签逛逛："
    )


def landing_keyboard(
    cloud: list[tuple[str, int]], total: int, store: QueryStore
) -> InlineKeyboardMarkup | None:
    """热门标签按钮，点了就是 `/c #标签`。

    覆盖超过一半帖子的标签不放（#漫画 662/938）—— 它什么也筛不掉。按占比判，
    不按名字判，换个频道也成立。
    """
    picks = [(t, n) for t, n in cloud if n * 2 <= total][:_LANDING_TAGS]
    if not picks:
        return None
    kb = InlineKeyboardBuilder()
    for tag, n in picks:
        kb.button(
            text=f"#{tag} {n}",
            callback_data=MANGA_PAGES.encode(0, f"#{tag}", store, strict=False),
        )
    kb.adjust(3)
    return kb.as_markup()
