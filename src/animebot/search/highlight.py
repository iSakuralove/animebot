"""关键词高亮。

只有一条规则，但它是整个模块存在的理由：**先在原文里定位，再逐段转义**。

顺序反了会出错：
  - 先转义再插标签 -> 要在 `&amp;` 里找关键词，位置全错
  - 先插标签再转义 -> `<b>` 自己被转义成 `&lt;b&gt;`，用户看到字面标签

而且不能用 `text.lower()` 的偏移直接切原文：`"İ".lower()` 是 2 个字符
（U+0069 U+0307），偏移会错位。所以匹配用 `casefold()` 的**逐字符等长映射**
—— 只对能等长小写化的字符做，其余原样比较。
"""

from __future__ import annotations

import html
import re

# 一次回复里最多插多少对标签。防止 '#奇幻' 这类命中 500 次把消息撑爆。
_MAX_MARKS = 40


def _fold(text: str) -> str:
    """等长小写化。长度必须与输入一致，否则偏移就不能用。

    `str.lower()` 对 U+0130（İ）会产出 2 个字符，所以逐字符处理并且丢弃
    长度变化的映射 —— 那种字符不参与大小写不敏感匹配，代价可以忽略。
    """
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def find_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """在原文里找出所有要高亮的区间，已合并重叠、已排序。

    合并重叠是必需的：搜 `无职 无职英雄` 两个词会命中同一段文字，不合并就会
    插出 `<b><b>无职</b>英雄</b>` 这种嵌套。
    """
    hay = _fold(text)
    spans: list[tuple[int, int]] = []
    for term in terms:
        needle = _fold(term)
        if not needle:
            continue
        start = 0
        while (i := hay.find(needle, start)) != -1:
            spans.append((i, i + len(needle)))
            start = i + len(needle)
            if len(spans) >= _MAX_MARKS:
                break

    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi:
            merged[-1] = (last_lo, max(last_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def highlight(text: str, terms: list[str], *, tag: str = "b") -> str:
    """转义 + 高亮。返回可直接塞进 HTML parse_mode 的字符串。

    没有命中时等价于 `html.escape(text)` —— 调用方不需要判断有没有关键词。
    """
    spans = find_spans(text, terms)
    if not spans:
        return html.escape(text)

    out: list[str] = []
    cursor = 0
    for lo, hi in spans:
        out.append(html.escape(text[cursor:lo]))
        out.append(f"<{tag}>{html.escape(text[lo:hi])}</{tag}>")
        cursor = hi
    out.append(html.escape(text[cursor:]))
    return "".join(out)


_HASHTAG = re.compile(r"#([0-9A-Za-z_一-鿿぀-ヿー]+)")


def query_terms(raw: str) -> list[str]:
    """从原始查询串里取出用于高亮的词。

    标签不参与高亮 —— `#奇幻` 是筛选条件，它不出现在标题里，高亮它只会在
    标签行制造一片粗体噪音。
    """
    return _HASHTAG.sub(" ", raw).split()
