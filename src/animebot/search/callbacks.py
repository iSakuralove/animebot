"""分页按钮的 callback_data 编解码。

Telegram 的 callback_data 上限是 **64 字节**（UTF-8 编码后），不是 64 字符。
一个汉字占 3 字节，所以能塞进去的中文查询词大约 19 个字。

绝大多数查询远小于这个数（番名 2~10 字，标签组合 30 字节左右），所以默认把
查询词直接编进 callback_data —— 无状态、重启不失效、不需要清理。

但超限是真实存在的，不是假想：库里就有

    英雄王，为了穷尽武道而转生～而后，成为世界最强的见习骑士♀～

30 个汉字 = 90 字节。用户直接粘贴标题搜索就会超。所以超限时退到短 token +
进程内存表。这条路径冷，所以必须有测试盯着，否则等真有人粘贴长标题时才发现。

两种形态共用一个 decode()，调用方不需要知道用了哪种。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

# Telegram 的硬限制
MAX_CALLBACK_BYTES = 64

PREFIX_INLINE = "s"   # s:<page>:<query>   查询词直接编进去
PREFIX_TOKEN = "t"    # t:<page>:<token>   查询词在 QueryStore 里
PREFIX_DETAIL = "p"   # p:<message_id>     打开详情
NOOP = "x"            # 占位按钮（当前页、已到边界）


@dataclass(frozen=True, slots=True)
class PageRef:
    page: int
    query: str


class QueryStore:
    """长查询词的进程内暂存。

    有界 LRU：超出容量就丢最旧的。丢掉的后果是用户点翻页看到「搜索已过期」，
    重新搜一次就行 —— 可恢复且有明确提示，比静默截断查询词返回错误结果好得多。

    不用 Redis：单实例部署，而且这是缓存不是业务状态，丢了不影响正确性。
    """

    def __init__(self, capacity: int = 512) -> None:
        self._cap = capacity
        self._items: OrderedDict[str, str] = OrderedDict()

    def put(self, query: str) -> str:
        """存查询词，返回 8 位 token。同一查询词永远得到同一个 token。"""
        token = hashlib.blake2s(query.encode(), digest_size=4).hexdigest()
        if token in self._items:
            self._items.move_to_end(token)
        else:
            self._items[token] = query
            while len(self._items) > self._cap:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> str | None:
        if (q := self._items.get(token)) is not None:
            self._items.move_to_end(token)
        return q

    def __len__(self) -> int:
        return len(self._items)


def encode_page(page: int, query: str, store: QueryStore) -> str:
    """page + query -> callback_data。塞得下就内联，塞不下就存表给 token。"""
    inline = f"{PREFIX_INLINE}:{page}:{query}"
    if len(inline.encode()) <= MAX_CALLBACK_BYTES:
        return inline
    return f"{PREFIX_TOKEN}:{page}:{store.put(query)}"


def decode_page(data: str, store: QueryStore) -> PageRef | None:
    """callback_data -> page + query。token 过期或格式不对返回 None。"""
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    kind, raw_page, tail = parts
    if not raw_page.isdigit():
        return None
    page = int(raw_page)
    if kind == PREFIX_INLINE:
        return PageRef(page=page, query=tail) if tail else None
    if kind == PREFIX_TOKEN:
        query = store.get(tail)
        return PageRef(page=page, query=query) if query else None
    return None
