"""帖子解析器。

设计要点（都是被真实数据逼出来的，不是偏好）：
- 先按行切出 key，再查别名表。绝不用 `if "字段名" in text` —— 那样 '开始' 会命中 '放送开始'。
- 简介模式下遇到未登记的 key 不退出，因为简介正文里也可能出现冒号。
- 链接从 entity 偏移按行归属，因为可见文本只有「点击下载」。
- 认不出来的 key 一律进 extra，永不丢弃。
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from typing import Any

from ..domain.fields import (
    BUTTON_ALIASES,
    LINK_LOOKUP,
    META_LOOKUP,
    NOISE_KEYS,
    SHARED_SHEET_IDS,
    STAFF_LOOKUP,
    normalize_key,
)
from ..domain.post import ParseStatus, Post
from .text import LinkRef, flatten, hashtags, sheet_id, split_kv

SUMMARY_HEADS = frozenset({"故事简介", "概况介绍", "剧情简介", "简介", "作品简介", "内容简介"})
_SCORE = re.compile(r"^([0-9]{1,2}(?:\.[0-9]+)?)\s*(.*)$")
_URL = re.compile(r"https?://\S+")
_DT_FMT = "%Y-%m-%dT%H:%M:%S"
_EPOCH = datetime.min.replace(tzinfo=UTC)

# 判为帖子所需的最低信号量
_STRONG = frozenset({"title_cn", "episodes", "index", "tags", "air_date"})


def _parse_dt(msg: dict[str, Any], key: str) -> datetime | None:
    """一律返回 aware UTC。

    优先读 `<key>_unixtime`（导出里覆盖率 100%）——它是无歧义的绝对时间。
    `date` 字段是**导出机器的本地时间**（这个频道的导出全是 +08:00），拿它当
    UTC 会让时间整体偏移 8 小时，而增量同步那边 aiogram 给的是真 UTC，两条
    入口就此错开。所有查询都 ORDER BY posted_at，错开的后果是新帖排错位置。
    """
    if (epoch := msg.get(f"{key}_unixtime")) is not None:
        with contextlib.suppress(ValueError, TypeError, OSError):
            return datetime.fromtimestamp(int(epoch), UTC)
    raw = msg.get(key)
    if not raw:
        return None
    with contextlib.suppress(ValueError, TypeError):
        # 兜底：没有 unixtime 的老导出。当本地时间处理再转 UTC。
        return datetime.strptime(raw, _DT_FMT).astimezone().astimezone(UTC)
    return None


def _is_summary_head(text: str) -> bool:
    return normalize_key(text) in SUMMARY_HEADS


def _apply_score(post: Post, value: str) -> None:
    mo = _SCORE.match(value)
    if mo:
        with contextlib.suppress(ValueError):
            post.score = float(mo.group(1))
        post.score_text = mo.group(2).strip()
    else:
        post.score_text = value


def _pick_url(value: str, line_links: list[LinkRef]) -> str | None:
    """优先取 entity 里的真 URL；退回 value 里的裸链接。"""
    if line_links:
        return line_links[0].url
    mo = _URL.search(value)
    return mo.group(0) if mo else None


def _apply_link(post: Post, canonical: str, url: str | None) -> None:
    if not url:
        return
    if canonical == "sheet":
        sid = sheet_id(url)
        if sid and sid in SHARED_SHEET_IDS:
            return  # 全频道公用的汇总表，放配置里，不入库
    post.links.setdefault(canonical, url)


def _apply_meta(post: Post, canonical: str, value: str) -> None:
    match canonical:
        case "title_cn":
            post.title_cn = post.title_cn or value
        case "title_en":
            post.title_en = post.title_en or value
        case "title_alias":
            post.aliases.extend(a.strip() for a in re.split(r"[、,，/]", value) if a.strip())
        case "episodes":
            post.episodes = post.episodes or value
        case "air_date":
            post.air_date = post.air_date or value
        case "air_weekday":
            post.air_weekday = post.air_weekday or value
        case "duration":
            post.duration = value
        case "score":
            if post.score is None:
                _apply_score(post, value)
        case "tags":
            post.tags.extend(t for t in hashtags(value) if t not in post.tags)
        case "index":
            post.index_tags.extend(t for t in hashtags(value) if t not in post.index_tags)
        case "password" | "password_alt":
            pw = value.strip()
            if pw and pw not in post.passwords:
                post.passwords.append(pw)
        case "extract_code" | "file_size":
            post.extra[canonical] = value
        case _:
            post.extra[canonical] = value


def _apply_buttons(post: Post, msg: dict[str, Any]) -> None:
    """inline 按钮只有 8.7% 的帖子有，但文字比正文 key 干净，作为补充来源。"""
    for row in msg.get("inline_bot_buttons") or []:
        for btn in row:
            if btn.get("type") != "url":
                continue
            canonical = BUTTON_ALIASES.get(normalize_key(btn.get("text", "")))
            if canonical:
                _apply_link(post, canonical, btn.get("data"))


def parse_message(msg: dict[str, Any], channel_id: int) -> Post | None:
    """解析一条导出消息。非 message 类型返回 None；不像帖子的返回 status=SKIPPED。"""
    if msg.get("type") != "message":
        return None

    full, links = flatten(msg)
    if not full.strip():
        return None

    by_line: dict[int, list[LinkRef]] = {}
    for ln in links:
        by_line.setdefault(ln.line, []).append(ln)

    post = Post(
        channel_id=channel_id,
        message_id=int(msg["id"]),
        posted_at=_parse_dt(msg, "date") or _EPOCH,
        edited_at=_parse_dt(msg, "edited"),
        has_photo="photo" in msg,
        raw_text=full,
    )

    hit_fields: set[str] = set()
    summary_lines: list[str] = []
    in_summary = False

    for idx, line in enumerate(full.split("\n")):
        stripped = line.strip()
        if not stripped:
            if in_summary and summary_lines:
                summary_lines.append("")
            continue

        # 独立成行的「故事简介」标题
        if _is_summary_head(stripped):
            in_summary = True
            continue

        kv = split_kv(line)
        if kv is None:
            if in_summary:
                summary_lines.append(stripped)
            elif not post.title_cn and idx <= 1 and 2 <= len(stripped) <= 120:
                post.title_cn = stripped  # 老帖子标题不带「中文名:」，取首行兜底
                post.parse_notes.append("title_from_first_line")
            continue

        raw_key, value = kv
        key = normalize_key(raw_key)

        if key in SUMMARY_HEADS:
            in_summary = True
            if value:
                summary_lines.append(value)
            continue

        if canonical := META_LOOKUP.get(key):
            in_summary = False
            hit_fields.add(canonical)
            _apply_meta(post, canonical, value)
        elif canonical := STAFF_LOOKUP.get(key):
            in_summary = False
            hit_fields.add(canonical)
            post.staff.setdefault(canonical, value)
        elif canonical := LINK_LOOKUP.get(key):
            in_summary = False
            hit_fields.add(canonical)
            _apply_link(post, canonical, _pick_url(value, by_line.get(idx, [])))
        elif in_summary:
            summary_lines.append(stripped)  # 简介正文里的冒号，不是字段
        elif not post.title_cn and idx <= 1 and 2 <= len(stripped) <= 200:
            # 'Re：从零开始的异世界生活' / '肌肉少女：哑铃，能举多少公斤？'
            # 首行的冒号是标题的一部分，不是字段分隔符。整行收下。
            post.title_cn = stripped
            post.parse_notes.append("title_from_first_line")
        elif key and key not in NOISE_KEYS:
            post.extra.setdefault(key, value)

    post.summary = "\n".join(summary_lines).strip()
    _apply_buttons(post, msg)
    post.parse_status = _classify(post, hit_fields)
    return post


def _classify(post: Post, hit: set[str]) -> ParseStatus:
    strong = hit & _STRONG
    if not post.title_cn and not strong:
        return ParseStatus.SKIPPED
    if not post.title_cn:
        return ParseStatus.FAILED
    if len(strong) >= 2:
        return ParseStatus.OK
    if strong or post.summary:
        return ParseStatus.PARTIAL
    return ParseStatus.SKIPPED
