"""/stop 指令测试。

盯住三件事：
  1. admin_only —— 普通用户不能停 bot（元数据层面，AccessMiddleware 实际拦截另有测试）
  2. 先回复、再停轮询 —— 回复没发出去就停等于用户不知道停没停
  3. 优雅停：设 stop_requested + 调 stop_polling，不硬杀（让 aclose 关 DB/HTTP）

runner 靠 dp["stop_requested"] 决定退出码 42（systemd 认它是主动停止不拉起），
所以这个 flag 必须被设上 —— 配错 bot 会被自动重启，/stop 形同虚设。
"""

from __future__ import annotations

import datetime as dt

import pytest
from aiogram.types import Chat, Message, User

from animebot.core.registry import spec_of
from animebot.features.system import handlers

NOW = dt.datetime(2026, 9, 19, 12, 0, 0, tzinfo=dt.UTC)


class FakeTrace:
    user_id = 913927421
    username = "admin"


class FakeDispatcher:
    """够用的假 dispatcher：记录 workflow_data 写入 + stop_polling 调用。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.events: list[str] = []

    def __setitem__(self, key: str, value: object) -> None:
        self.data[key] = value
        self.events.append(f"set:{key}")

    async def stop_polling(self) -> None:
        self.events.append("stop_polling")


def make_message(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> Message:
    async def fake_reply(self: Message, text: str, **kw: object) -> None:
        sent.append(text)
        # 记录回复相对 stop_polling 的先后
        _ORDER.append("reply")

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    return Message(
        message_id=1, date=NOW,
        chat=Chat(id=913927421, type="private"),
        from_user=User(id=913927421, is_bot=False, first_name="A"),
        text="/stop",
    )


_ORDER: list[str] = []


def test_stop_is_admin_only() -> None:
    """/stop 必须 admin_only —— 谁都能停 bot 是灾难。"""
    spec = spec_of(handlers.cmd_stop)
    assert spec is not None
    assert spec.admin_only is True


async def test_stop_sets_flag_and_stops_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    _ORDER.clear()
    sent: list[str] = []
    msg = make_message(monkeypatch, sent)
    dp = FakeDispatcher()

    await handlers.cmd_stop(msg, FakeTrace(), dp)

    assert dp.data.get("stop_requested") is True, "没设 stop_requested，systemd 会自动拉起"
    assert "stop_polling" in dp.events
    assert sent and "停止" in sent[0]


async def test_stop_replies_before_stopping_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回复必须在 stop_polling 之前 —— 否则 polling 停了、确认发不出去。"""
    _ORDER.clear()
    sent: list[str] = []
    msg = make_message(monkeypatch, sent)

    class OrderedDispatcher(FakeDispatcher):
        async def stop_polling(self) -> None:
            _ORDER.append("stop_polling")

    await handlers.cmd_stop(msg, FakeTrace(), OrderedDispatcher())
    assert _ORDER == ["reply", "stop_polling"]
