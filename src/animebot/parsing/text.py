"""Telegram 导出 JSON 的文本层处理。

两个绕不过去的事实：
1. `text` 在有 entity 时是 [str | {type,text}] 混排数组，直接当字符串用会 TypeError。
2. 下载链接的锚文本是「点击下载」「打开」，甚至是单个 "\n"（隐藏链接），
   URL 只存在于 entity 的 href 里。所以要把 URL 和它所在行的字段名对应起来，
   必须记录 entity 在拍平文本中的偏移 —— 靠正则猜是猜不出来的。
"""

from __future__ import annotations

import bisect
import dataclasses
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LinkRef:
    url: str
    anchor: str          # 可见锚文本，可能是 "点击下载" 或 "\n"
    start: int           # 在拍平文本中的起始偏移
    line: int = -1       # 所属行号（拍平后按 \n 计）


def _entities(msg: dict[str, Any]) -> list[Any]:
    """优先 text_entities（plain 也被包成对象，结构统一），退回 text。"""
    ents = msg.get("text_entities")
    if ents:
        return ents
    raw = msg.get("text")
    if isinstance(raw, str):
        return [{"type": "plain", "text": raw}]
    return raw or []


def _line_starts(text: str) -> list[int]:
    starts = [0]
    idx = text.find("\n")
    while idx != -1:
        starts.append(idx + 1)
        idx = text.find("\n", idx + 1)
    return starts


def flatten(msg: dict[str, Any]) -> tuple[str, list[LinkRef]]:
    """拍平成纯文本，同时返回所有链接及其行号。"""
    buf: list[str] = []
    links: list[LinkRef] = []
    pos = 0
    for e in _entities(msg):
        if isinstance(e, str):
            buf.append(e)
            pos += len(e)
            continue
        txt = e.get("text") or ""
        url = e.get("href") or (txt if e.get("type") == "link" else None)
        if url and url.startswith(("http://", "https://", "tg://")):
            links.append(LinkRef(url=url, anchor=txt, start=pos))
        buf.append(txt)
        pos += len(txt)

    full = "".join(buf)
    starts = _line_starts(full)
    resolved = [
        dataclasses.replace(ln, line=bisect.bisect_right(starts, ln.start) - 1)
        for ln in links
    ]
    return full, resolved


def plain_text(msg: dict[str, Any]) -> str:
    return flatten(msg)[0]


_HASHTAG = re.compile(r"#([0-9A-Za-z_一-鿿぀-ヿー]+)")


def hashtags(line: str) -> list[str]:
    """hashtag 的文本本身就在拍平结果里，直接抓，不必走 entity。"""
    return _HASHTAG.findall(line)


_KV = re.compile(r"^\s*([^\s:：]{1,14}?)\s*[:：]\s*(.*)$")


def split_kv(line: str) -> tuple[str, str] | None:
    """把 '☺️评分：4.9 不过不失' 切成 ('☺️评分', '4.9 不过不失')。

    先切 key 再查表，而不是拿字段名去 `in text` —— 后者会让 '开始' 命中 '放送开始'。
    """
    mo = _KV.match(line)
    if not mo:
        return None
    return mo.group(1), mo.group(2).strip()


_SHEET_ID = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


def sheet_id(url: str) -> str | None:
    mo = _SHEET_ID.search(url)
    return mo.group(1) if mo else None
