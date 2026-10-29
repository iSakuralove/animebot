"""把模型吐的 Markdown 转成 Telegram 的 HTML。

为什么不直接 `parse_mode=MarkdownV2`：MarkdownV2 要求转义 18 个字符，模型
漏一个整个消息就 400；而且它不支持标题、代码块里的语言标记会原样显示。HTML
只要求转义 `& < >`，容错好得多。

支持的子集是模型实际会用的：`**粗体**`、`*斜体*`（和 `_斜体_`）、`` `代码` ``、
```围栏```、`[文字](url)`、`# 标题`、`- 列表`、`> 引用`。其余原样转义 ——
猜错比不转换更糟（模型写 `a < b` 时不该变成一个标签）。

一条硬约束：**链接 URL 只保留 http/https/tg**。模型可能编出 `javascript:` 或
`file://`，转进 `<a href>` 就是可点击的坑。
"""

from __future__ import annotations

import html
import re

_SAFE_SCHEME = re.compile(r"^(https?://|tg://)", re.I)
_CODE_FENCE = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_BULLET = re.compile(r"^[ \t]*[-*+]\s+", re.M)
_QUOTE = re.compile(r"^>\s?", re.M)
_HR = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$", re.M)
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$", re.M)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _link(match: re.Match[str]) -> str:
    label, url = match.group(1), match.group(2)
    url = html.unescape(url)
    if not _SAFE_SCHEME.match(url):
        return f"{label}（{url}）"     # 不安全/相对链接退成纯文本，不生成 <a>
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def flatten_tables(text: str) -> str:
    """把 markdown 表格压成「· a | b | c」的行。

    Telegram 不渲染表格 —— 原样发出去就是一堆竖线。系统提示里已经要求模型
    别写表格，但模型不一定听，所以这里兜一层。

    对齐分隔行（`|---|:--:|`）直接丢掉；其余每行取单元格、去掉首尾空白。
    """
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _TABLE_SEP.match(line):
            continue
        row = _TABLE_ROW.match(line)
        if row:
            cells = [cell.strip() for cell in row.group(1).split("|")]
            cells = [cell for cell in cells if cell]
            if cells:
                out.append("· " + " | ".join(cells))
                continue
        out.append(line)
    return "\n".join(out)


def to_telegram_html(markdown: str) -> str:
    """Markdown 子集 -> Telegram HTML。处理顺序是设计的一部分：

    先摘出代码块（内容整体转义、不参与任何行内替换），再转义正文，最后才插
    标签。顺序反了会把 `<b>` 自己转义掉，或者在 `&amp;` 里找 `**`。
    """
    if not markdown:
        return ""

    blocks: list[str] = []

    def stash(block: str) -> str:
        blocks.append(block)
        return f"\x00{len(blocks) - 1}\x00"

    def code_block(match: re.Match[str]) -> str:
        body = _escape(match.group(2).strip("\n"))
        return stash(f"<pre>{body}</pre>")

    text = _CODE_FENCE.sub(code_block, flatten_tables(markdown))

    def inline_code(match: re.Match[str]) -> str:
        return stash(f"<code>{_escape(match.group(1))}</code>")

    text = _INLINE_CODE.sub(inline_code, text)

    # 正文转义。之后所有的标签插入都基于已转义文本，安全。
    text = _escape(text)
    text = _HR.sub("———", text)
    text = _HEADING.sub(lambda m: f"<b>{m.group(2).strip()}</b>", text)
    text = _QUOTE.sub("▎", text)
    text = _BULLET.sub("· ", text)

    text = _LINK.sub(_link, text)          # 在转义之后：URL 里的 & 已经是 &amp;
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER.sub(r"<i>\1</i>", text)

    # 还原代码块。占位符里只有数字，转义不会动它。
    for index, block in enumerate(blocks):
        text = text.replace(f"\x00{index}\x00", block)
    return text.strip()
