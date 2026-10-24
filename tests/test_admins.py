"""/admins：远程增删查管理员名单。

两层各测一遍：AdminStore 的增删 + .env 原子落盘（机制），以及指令走完整
中间件链之后「加完立即有权限、删完立即没权限」（效果）。最后一条最重要 ——
改名单不用重启是这个功能的全部意义。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from animebot.bot.middlewares import (
    AccessMiddleware,
    ErrorBoundary,
    ObservabilityMiddleware,
    RateLimitMiddleware,
    TraceMiddleware,
)
from animebot.config import AdminStore, Settings
from animebot.core.registry import CommandRegistry
from animebot.features.system.handlers import cmd_admins, cmd_ping

ADMIN = 42
NEWCOMER = 777
NOW = dt.datetime(2026, 10, 24, 12, 0, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------- AdminStore


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "AnimeCoffeebot=secret\nANIMEBOT_ADMIN_IDS=42,100\nANIMEBOT_LINK_MODE=internal\n",
        encoding="utf-8",
    )
    return p


def test_list_and_membership(env_file: Path) -> None:
    store = AdminStore([42, 100, 42], env_file)   # 重复 id 去重
    assert store.ids == (42, 100)
    assert 42 in store and 999 not in store
    assert len(store) == 2


def test_add_persists_to_env_without_touching_other_lines(env_file: Path) -> None:
    store = AdminStore([42, 100], env_file)
    assert store.add(777) is True
    assert store.add(777) is False, "重复 add 幂等，不重写文件"

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert "ANIMEBOT_ADMIN_IDS=42,100,777" in lines
    assert lines[0] == "AnimeCoffeebot=secret", "别的行被动了"
    assert "ANIMEBOT_LINK_MODE=internal" in lines


def test_remove_persists(env_file: Path) -> None:
    store = AdminStore([42, 100], env_file)
    assert store.remove(100) is True
    assert store.remove(100) is False
    assert "ANIMEBOT_ADMIN_IDS=42" in env_lines(env_file)


def test_persist_appends_when_key_missing(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("AnimeCoffeebot=secret\n", encoding="utf-8")
    AdminStore([], p).add(42)
    assert p.read_text(encoding="utf-8").splitlines()[-1] == "ANIMEBOT_ADMIN_IDS=42"


def test_store_never_touches_disk_until_mutation(tmp_path: Path) -> None:
    """只构造不增删就不读写文件 —— 测试和只读环境里零副作用。"""
    AdminStore([1, 2], tmp_path / "不存在.env")
    assert not (tmp_path / "不存在.env").exists()


# ---------------------------------------------------------------- 指令全链路


class Recorder:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()

    async def fake_reply(self: Message, text: str, **_kw: object) -> None:
        r.replies.append(text)

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    return r


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def store(env_file: Path) -> AdminStore:
    return AdminStore([ADMIN, 100], env_file)


@pytest.fixture
def dp(settings: Settings, store: AdminStore) -> Dispatcher:
    from animebot.bot.wiring import bind_commands, spec_of

    reg = CommandRegistry()
    router = Router(name="ops")
    bind_commands(router, [spec_of(cmd_admins), spec_of(cmd_ping)], reg, "system")

    d = Dispatcher()
    d["registry"] = reg
    d["admins"] = store
    d["settings"] = settings
    for mw in (
        TraceMiddleware(reg),
        ErrorBoundary(),
        AccessMiddleware(reg, store),
        RateLimitMiddleware(reg),
        ObservabilityMiddleware(settings),
    ):
        d.update.outer_middleware(mw)
    d.include_router(router)
    return d


def env_lines(p: Path) -> list[str]:
    """同步读 .env —— async 测试里直接 Path.read_text 会被 ASYNC240 拦。"""
    return p.read_text(encoding="utf-8").splitlines()


def update(text: str, *, user_id: int, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        text=text,
    ))


async def test_lists_ids(dp: Dispatcher, fake_bot: Bot, rec: Recorder) -> None:
    await dp.feed_update(fake_bot, update("/admins", user_id=ADMIN))
    assert rec.replies == ["管理员（2）: 42、100"]


async def test_non_admin_cannot_even_ask(dp: Dispatcher, fake_bot: Bot, rec: Recorder) -> None:
    await dp.feed_update(fake_bot, update("/admins", user_id=NEWCOMER))
    assert rec.replies == ["你没有权限使用这个指令"]


async def test_add_takes_effect_immediately(
    dp: Dispatcher, fake_bot: Bot, rec: Recorder, store: AdminStore, env_file: Path
) -> None:
    """/admins add 之后，新管理员当场就能用 /ping —— 不重启，这正是意义所在。"""
    await dp.feed_update(fake_bot, update("/admins add 777", user_id=ADMIN))
    assert "已添加 777" in rec.replies[-1]
    assert store.ids == (42, 100, 777)
    assert "ANIMEBOT_ADMIN_IDS=42,100,777" in env_lines(env_file)

    rec.replies.clear()
    await dp.feed_update(fake_bot, update("/ping", user_id=NEWCOMER))
    assert rec.replies[0].startswith("pong"), "加完立即有权限"


async def test_add_duplicate_says_so(dp: Dispatcher, fake_bot: Bot, rec: Recorder) -> None:
    await dp.feed_update(fake_bot, update("/admins add 100", user_id=ADMIN))
    assert rec.replies == ["100 已经是管理员了"]


async def test_remove_revokes_immediately(
    dp: Dispatcher, fake_bot: Bot, rec: Recorder, store: AdminStore, env_file: Path
) -> None:
    await dp.feed_update(fake_bot, update("/admins del 100", user_id=ADMIN))
    assert "已删除 100" in rec.replies[-1]
    assert 100 not in store
    assert "ANIMEBOT_ADMIN_IDS=42" in env_lines(env_file)

    rec.replies.clear()
    await dp.feed_update(fake_bot, update("/ping", user_id=100))
    assert rec.replies == ["你没有权限使用这个指令"], "删完立即失效"


async def test_remove_missing_and_last_guard(
    dp: Dispatcher, fake_bot: Bot, rec: Recorder, store: AdminStore
) -> None:
    await dp.feed_update(fake_bot, update("/admins del 999", user_id=ADMIN))
    assert "不在管理员名单" in rec.replies[-1]

    # 删到只剩一个时拒绝 —— 全删了没人能进后台
    await dp.feed_update(fake_bot, update("/admins del 100", user_id=ADMIN, update_id=2))
    assert store.ids == (42,)
    rec.replies.clear()
    await dp.feed_update(fake_bot, update("/admins del 42", user_id=ADMIN, update_id=3))
    assert "至少保留一个管理员" in rec.replies[-1]
    assert store.ids == (42,)


async def test_bad_usage(dp: Dispatcher, fake_bot: Bot, rec: Recorder) -> None:
    await dp.feed_update(fake_bot, update("/admins frobnicate", user_id=ADMIN))
    assert "用法" in rec.replies[-1]
    await dp.feed_update(fake_bot, update("/admins add abc", user_id=ADMIN, update_id=2))
    assert "用法" in rec.replies[-1]
