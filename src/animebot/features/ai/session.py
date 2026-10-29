"""多轮对话的上下文。

键是 `(chat_id, user_id)` —— 群里两个人同时问不应该互相看到对方的上下文，
私聊里 chat_id 就等于 user_id，两种场景用同一套键不会冲突。

进程内存 + 闲置过期，不做持久化：

- 重启后上下文丢失是可以接受的（用户重问一次），而落库要处理清理、迁移、
  和「同一个人在三个群里各有一份」的复杂度；
- 上限按条数截断而不是按 token 数 —— 我们拿不到准确的分词，按条数保守截断
  （默认 16 条）比按字符估更不容易把请求撑爆。

截断保留**最近**的消息，并且以 `user` 开头 —— Claude 协议要求 messages 必须
user/assistant 交替且以 user 起头，从中间截断会得到 400。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

Key = tuple[int, int]


@dataclass(slots=True)
class Conversation:
    messages: list[dict[str, Any]] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def add_assistant(self, content: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "user", "content": results})

    def trimmed(self, limit: int) -> list[dict[str, Any]]:
        """截到最近 `limit` 条，并从一条完整的 user 回合开始。

        tool_result 也是 user 角色 —— 它前面必须有对应的 assistant tool_use，
        所以在它处开始会让协议不成立。找到一个「纯 text 的 user 消息」当起点。
        """
        msgs = self.messages[-limit:] if limit > 0 else list(self.messages)
        for index, msg in enumerate(msgs):
            if msg["role"] != "user":
                continue
            blocks = msg.get("content") or []
            if any(block.get("type") == "text" for block in blocks):
                return msgs[index:]
        return []


class ConversationStore:
    """有界 + 闲置过期的会话表。单实例部署，够用。"""

    def __init__(self, *, ttl: float = 1800.0, max_keys: int = 256) -> None:
        self._ttl = ttl
        self._max = max_keys
        self._items: OrderedDict[Key, Conversation] = OrderedDict()

    def get(self, key: Key) -> Conversation:
        now = time.monotonic()
        conv = self._items.get(key)
        if conv is not None and now - conv.touched > self._ttl:
            del self._items[key]
            conv = None
        if conv is None:
            conv = Conversation(touched=now)
            self._items[key] = conv
            while len(self._items) > self._max:
                self._items.popitem(last=False)
        conv.touched = now
        self._items.move_to_end(key)
        return conv

    def reset(self, key: Key) -> bool:
        return self._items.pop(key, None) is not None

    def prune(self) -> int:
        now = time.monotonic()
        dead = [k for k, v in self._items.items() if now - v.touched > self._ttl]
        for k in dead:
            del self._items[k]
        return len(dead)

    def __len__(self) -> int:
        return len(self._items)
