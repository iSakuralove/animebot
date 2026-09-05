"""把 aiogram 的 Message 转成导出 JSON 的形状，喂给同一个 `parse_message`。

为什么绕一圈而不是写第二个解析器：两份解析代码必然漂移 —— 回填修的 bug
增量里还在，反之亦然。1486 个帖子的黄金回归测试只覆盖回填那条路，第二个
解析器等于零测试覆盖。

转换只做形状，不做语义判断。「这是不是我的频道」「这是不是帖子」分别由
`sync` 服务和 `parse_message` 负责。

放在 ingest/ 而不是 parsing/：它 import aiogram，而 parsing 那一层不该
知道 aiogram 存在。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram.types import Message, MessageEntity

# 导出 JSON 只保留这几种 entity 的语义信息，其余一律降级成 plain。
# 解析器实际只用到 href 和文本内容，type 只在 "link" 上有分支。
_URL_TYPES = frozenset({"text_link", "url"})


def _entity_to_export(ent: MessageEntity, text: str) -> dict[str, Any]:
    """一个 entity -> 导出 JSON 的一段。

    切片必须用 `ent.extract_from()`：Bot API 的 offset/length 按 **UTF-16 码元**
    计，而 Python 字符串按码位。帖子正文里 emoji 密集（`💙故事简介`、`🔐解压`、
    `😱百度网盘`），一个 emoji 占 2 个码元但 `len()` 是 1 —— 直接拿 offset 当
    Python 索引会让后面所有偏移错位，链接归属到错误的行上。
    """
    seg = ent.extract_from(text)
    if ent.type == "text_link":
        return {"type": "text_link", "text": seg, "href": ent.url or ""}
    if ent.type == "url":
        # 裸链接：导出 JSON 里 type 是 "link"，文本本身就是 URL
        return {"type": "link", "text": seg}
    return {"type": "plain", "text": seg}


def _build_entities(text: str, entities: list[MessageEntity] | None) -> list[dict[str, Any]]:
    """按 UTF-16 偏移顺序重建 text_entities，中间的空隙补成 plain 段。

    必须补空隙：`flatten()` 是靠把所有段拼起来还原全文的，漏掉一段就会让
    后续所有链接的行号算错。
    """
    if not entities:
        return [{"type": "plain", "text": text}] if text else []

    # aiogram 不保证 entities 有序（Telegram 实际是有序的，但不能依赖）
    ordered = sorted(entities, key=lambda e: (e.offset, e.length))
    out: list[dict[str, Any]] = []
    cursor = 0  # UTF-16 码元游标
    for ent in ordered:
        if ent.offset < cursor:
            continue  # 嵌套/重叠 entity（如 bold 套在 text_link 里），外层已覆盖
        if ent.offset > cursor:
            gap = MessageEntity(type="plain", offset=cursor, length=ent.offset - cursor)
            out.append({"type": "plain", "text": gap.extract_from(text)})
        out.append(_entity_to_export(ent, text))
        cursor = ent.offset + ent.length

    tail = MessageEntity(type="plain", offset=cursor, length=_utf16_len(text) - cursor)
    if tail.length > 0:
        out.append({"type": "plain", "text": tail.extract_from(text)})
    return out


def _utf16_len(text: str) -> int:
    """UTF-16 码元长度。Telegram 的 offset/length 全部用这个单位。"""
    return len(text.encode("utf-16-le")) // 2


def _buttons_to_export(msg: Message) -> list[list[dict[str, Any]]]:
    """reply_markup.inline_keyboard -> inline_bot_buttons。

    导出 JSON 用 {"type": "url", "text": ..., "data": <url>}，
    Bot API 用 InlineKeyboardButton(text=..., url=...)。字段名不一样。
    """
    markup = msg.reply_markup
    if markup is None or not markup.inline_keyboard:
        return []
    rows: list[list[dict[str, Any]]] = []
    for row in markup.inline_keyboard:
        cells = [
            {"type": "url", "text": btn.text, "data": btn.url}
            for btn in row
            if btn.url
        ]
        if cells:
            rows.append(cells)
    return rows


def _epoch(value: dt.datetime | int | float | None) -> int | None:
    """归一化成 Unix 秒。

    aiogram 3.31 的字段类型不一致：`Message.date` 是 `datetime`，而
    `Message.edit_date` 是 `int`（`forward_date` 又是 datetime）。假设错了会在
    「被编辑过的帖子」上抛 AttributeError —— 而这个频道 1486/1486 的帖子都编辑
    过，等于每一条增量都炸。所以两种都接。
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return int(value.timestamp())
    return int(value)


def message_to_export_shape(msg: Message) -> dict[str, Any]:
    """aiogram Message -> 导出 JSON 里一条 message 的形状。

    只填 `parse_message` 会读的键。多填无害但没用，少填会静默丢字段，
    所以下面每一个键都对应解析器里的一次读取。
    """
    text = msg.text or msg.caption or ""
    entities = msg.entities if msg.text else msg.caption_entities

    shape: dict[str, Any] = {
        "id": msg.message_id,
        "type": "message",
        "text": text,
        "text_entities": _build_entities(text, entities),
        # 时间统一走 unixtime：解析器优先读它，天然是 UTC，不经过本地时区
        "date_unixtime": _epoch(msg.date),
    }
    if (edited := _epoch(msg.edit_date)) is not None:
        shape["edited_unixtime"] = edited
    if msg.photo:
        # 解析器只看这个键存在与否（has_photo），不看值。
        # 不存 file_id：它是 per-bot 凭证，换 bot 就失效，存了也没用。
        shape["photo"] = "(live update)"
    if buttons := _buttons_to_export(msg):
        shape["inline_bot_buttons"] = buttons
    return shape
