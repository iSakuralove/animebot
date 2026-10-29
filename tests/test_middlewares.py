"""中间件与装配的集成测试。

不连 Telegram：手搓 Update 对象喂进 Dispatcher，验证 trace 注入、
错误边界、限流、权限、埋点是不是真的生效。这是「加了新模块会不会漏保护」
的唯一可靠答案。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

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
from animebot.core.errors import UsageError
from animebot.core.registry import CommandRegistry, command, spec_of
from animebot.observability.context import RequestContext
from animebot.observability.metrics import METRICS

USER_ID = 1001
ADMIN_ID = 42
CHAT_ID = -1001702674582


def make_update(text: str, *, user_id: int = USER_ID, chat_type: str = "private",
                update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.UTC),
            chat=Chat(id=CHAT_ID if chat_type != "private" else user_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            text=text,
        ),
    )


class Recorder:
    """假的 Message.reply / answer：把回复内容记下来而不是真发出去。

    `answer` 也要换 —— 群聊里的回复先走 ephemeral（`Message.answer` + 接收者），
    只替换 `reply` 的话会真的打 Telegram，测试变成联网测试。
    """

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()

    async def fake_reply(self: Message, text: str, **kw: object) -> None:
        await r.reply(text)

    async def fake_answer(self: Message, text: str, **kw: object) -> None:
        await r.reply(text)

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    monkeypatch.setattr(Message, "answer", fake_answer, raising=True)
    return r


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    """真 Bot 对象但从不发请求：Message.reply 已被替换，Dispatcher 只需要 bot.id。"""
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


def build_dp(registry: CommandRegistry, router: Router, settings) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["registry"] = registry
    for mw in (
        TraceMiddleware(registry),
        ErrorBoundary(),
        AccessMiddleware(registry, settings.admin_ids),
        RateLimitMiddleware(registry),
        ObservabilityMiddleware(settings),
    ):
        dp.update.outer_middleware(mw)
    dp.include_router(router)
    return dp


def wire(registry: CommandRegistry, *handlers: object) -> Router:
    """等价于真实装配路径，只是不经过 feature 包。"""
    from animebot.bot.wiring import bind_commands

    router = Router(name="test")
    specs = []
    for fn in handlers:
        spec = spec_of(fn)
        assert spec is not None
        specs.append(spec)
    bind_commands(router, specs, registry, "test")
    return router


class TestTraceInjection:
    async def test_handler_receives_trace(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        seen: list[RequestContext] = []

        @command("probe", desc="探针")
        async def probe(message: Message, trace: RequestContext) -> None:
            seen.append(trace)
            await message.reply("ok")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, probe), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/probe hello"))

        assert len(seen) == 1
        ctx = seen[0]
        assert ctx.command == "probe"
        assert ctx.feature == "test"
        assert ctx.user_id == USER_ID
        assert ctx.payload == "hello"
        assert len(ctx.trace_id) == 8

    async def test_handler_without_trace_param_still_works(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        """handler 只声明它要的参数，多余的 data 键不该炸。"""

        @command("bare")
        async def bare(message: Message) -> None:
            await message.reply("bare ok")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, bare), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/bare"))
        assert rec.replies == ["bare ok"]


class TestErrorBoundary:
    async def test_user_error_shows_message(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        @command("needarg")
        async def needarg(message: Message) -> None:
            raise UsageError("要搜什么？", "/needarg 关键词")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, needarg), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/needarg"))

        assert len(rec.replies) == 1
        assert "要搜什么？" in rec.replies[0]
        assert "/needarg 关键词" in rec.replies[0]

    async def test_crash_returns_trace_id_not_stacktrace(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        """真 bug 给用户一个编号，不喷栈追踪。"""

        @command("boom")
        async def boom(message: Message) -> None:
            raise ZeroDivisionError("division by zero")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, boom), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/boom"))

        assert len(rec.replies) == 1
        reply = rec.replies[0]
        assert "错误编号" in reply
        assert "ZeroDivisionError" not in reply
        assert "Traceback" not in reply

    async def test_crash_does_not_propagate(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        """一个指令崩了不能拖垮 polling 循环。"""

        @command("boom2")
        async def boom2(message: Message) -> None:
            raise RuntimeError("炸了")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, boom2), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/boom2"))
        # 没抛出来就是对的


class TestAccess:
    async def test_admin_only_blocks_normal_user(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        @command("root", admin_only=True)
        async def root(message: Message) -> None:
            await message.reply("机密")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, root), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/root"))

        assert "没有权限" in rec.replies[0]
        assert "机密" not in rec.replies[0]

    async def test_admin_passes(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        @command("root2", admin_only=True)
        async def root2(message: Message) -> None:
            await message.reply("机密")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, root2), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/root2", user_id=ADMIN_ID)
        )
        assert rec.replies == ["机密"]

    async def test_group_disallowed(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        @command("privonly", group_allowed=False)
        async def privonly(message: Message) -> None:
            await message.reply("私聊内容")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, privonly), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/privonly", chat_type="supergroup")
        )
        assert "只能在私聊" in rec.replies[0]


class TestRateLimit:
    async def test_blocks_after_quota(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        calls: list[int] = []

        @command("limited", rate=(2, 60))
        async def limited(message: Message) -> None:
            calls.append(1)
            await message.reply("done")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, limited), settings)
        for i in range(4):
            await dp.feed_update(bot=fake_bot, update=make_update("/limited", update_id=i + 1)
            )

        assert len(calls) == 2, "限流没生效"
        assert sum("太频繁" in r for r in rec.replies) == 2

    async def test_unlimited_command_not_affected(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        calls: list[int] = []

        @command("free")
        async def free(message: Message) -> None:
            calls.append(1)
            await message.reply("ok")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, free), settings)
        for i in range(5):
            await dp.feed_update(bot=fake_bot, update=make_update("/free", update_id=i + 1)
            )
        assert len(calls) == 5

    async def test_per_user_isolation(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        calls: list[int] = []

        @command("peruser", rate=(1, 60))
        async def peruser(message: Message) -> None:
            calls.append(message.from_user.id)  # type: ignore[union-attr]
            await message.reply("ok")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, peruser), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/peruser", user_id=1, update_id=1))
        await dp.feed_update(bot=fake_bot, update=make_update("/peruser", user_id=2, update_id=2))
        assert calls == [1, 2], "一个用户的限流影响到了另一个"


class TestMetricsIntegration:
    async def test_counts_and_times(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        METRICS.reset()

        @command("measured")
        async def measured(message: Message) -> None:
            await message.reply("ok")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, measured), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/measured"))

        snap = METRICS.snapshot()
        assert any("measured" in k and "outcome=ok" in k for k in snap["counters"])
        assert any("measured" in k for k in snap["timings"])

    async def test_error_outcome_labeled(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        METRICS.reset()

        @command("failing")
        async def failing(message: Message) -> None:
            raise UsageError("参数错")

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, failing), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/failing"))

        keys = METRICS.snapshot()["counters"]
        assert any("outcome=user_error" in k for k in keys)


class TestCommandPolicy:
    """权限策略钉在测试里：哪边加了指令忘了标（或标错边）就红。

    成员的入口只有检索：私聊直接发字、/s、/c、/help。
    资料类（/check /tags）和运维类（/ping /trace /health /metrics /syncstat）
    都是管理员专用 —— 频道主 2026-10-24 的决定。
    """

    async def test_admin_only_commands(self) -> None:
        from animebot.features.search import handlers as search
        from animebot.features.system import handlers as system

        admin_cmds = [getattr(system, f"cmd_{n}")
                      for n in ("ping", "trace", "health", "metrics", "syncstat")]
        admin_cmds += [search.cmd_check, search.cmd_tags]
        for fn in admin_cmds:
            spec = spec_of(fn)
            assert spec is not None
            assert spec.admin_only, f"/{spec.name} 必须只能管理员用"

    async def test_member_search_commands_stay_open(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        """检索入口对所有人开放 —— 收紧权限时别把它们一起锁死。"""
        from animebot.features.manga import handlers as manga
        from animebot.features.search import handlers as search

        # 纯文本搜索没有 @command 元数据（单独注册），天然无权限门，不在断言之列
        for fn in (search.cmd_search, manga.cmd_manga):
            spec = spec_of(fn)
            assert spec is not None
            assert not spec.admin_only, f"/{spec.name} 是给频道成员用的，不该 admin_only"


class TestAdminVisibility:
    """管理员要能「看见」自己的专用指令：/help 列表 + 输入框菜单。

    权限本身早已由 admin_only 保证，这里测的是可见性 —— 菜单对全员隐藏
    admin_only 指令是给普通用户的，不该把管理员一起挡在外面。
    """

    async def test_help_shows_admin_section_to_admin_only(
        self, settings, rec: Recorder, fake_bot: Bot
    ) -> None:
        from animebot.features.system.handlers import cmd_help, cmd_ping

        reg = CommandRegistry()
        dp = build_dp(reg, wire(reg, cmd_help, cmd_ping), settings)
        await dp.feed_update(bot=fake_bot, update=make_update("/help"))          # 普通用户
        await dp.feed_update(
            bot=fake_bot, update=make_update("/help", user_id=ADMIN_ID)          # 管理员
        )
        normal, admin = rec.replies[-2], rec.replies[-1]
        assert "/ping" not in normal, "普通用户不该看到专用指令"
        assert "/ping" in admin and "🔒" in admin, "管理员要能看到专用指令（带 🔒）"

    async def test_menu_scopes(
        self, settings, fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """三层菜单：默认 scope（私聊）、所有群聊（ephemeral）、管理员私聊。"""
        import types as t

        from aiogram.types import (
            BotCommandScopeAllGroupChats,
            BotCommandScopeChat,
            BotCommandScopeDefault,
        )

        from animebot.bot.app import sync_bot_commands

        @command("open_cmd", desc="成员用")
        async def open_cmd(message: Message) -> None: ...

        @command("public_cmd", desc="答案公开", group_ephemeral=False)
        async def public_cmd(message: Message) -> None: ...

        @command("adm_cmd", desc="管理员用", admin_only=True)
        async def adm_cmd(message: Message) -> None: ...

        @command("dbg", desc="调试", hidden=True, admin_only=True)
        async def dbg(message: Message) -> None: ...

        reg = CommandRegistry()
        from animebot.bot.wiring import bind_commands
        bind_commands(
            Router(),
            [spec_of(open_cmd), spec_of(public_cmd), spec_of(adm_cmd), spec_of(dbg)],
            reg, "t",
        )

        calls: list[object] = []

        async def fake_call(self: Bot, method: object, **_kw: object) -> object:
            calls.append(method)
            return True

        monkeypatch.setattr(type(fake_bot), "__call__", fake_call, raising=True)

        cfg = settings.model_copy(update={"admin_ids": (ADMIN_ID,)})
        app = t.SimpleNamespace(bot=fake_bot, registry=reg, settings=cfg)
        await sync_bot_commands(app)

        set_calls = [c for c in calls if type(c).__name__ == "SetMyCommands"]
        assert len(set_calls) == 3, "默认 scope + 所有群聊 + 管理员私聊"

        default = next(c for c in set_calls
                       if isinstance(c.scope, BotCommandScopeDefault))
        group = next(c for c in set_calls
                     if isinstance(c.scope, BotCommandScopeAllGroupChats))
        admin = next(c for c in set_calls
                     if isinstance(c.scope, BotCommandScopeChat))
        assert admin.scope.chat_id == ADMIN_ID
        assert [b.command for b in default.commands] == ["open_cmd", "public_cmd"]
        assert [b.command for b in admin.commands] == [
            "open_cmd", "public_cmd", "adm_cmd"
        ], "管理员多看到 admin_only，hidden 的对谁都不进菜单"
        assert [b.command for b in group.commands] == ["open_cmd", "public_cmd"]

        flags = {b.command: b.is_ephemeral for b in group.commands}
        assert flags["open_cmd"] is True, "默认标 ephemeral：用户发的消息别人看不见"
        assert flags["public_cmd"] is None, (
            "group_ephemeral=False 的不标 —— 答案公开的，问题也得公开"
        )
        assert all(b.is_ephemeral is not True for b in default.commands), (
            "私聊菜单不该标 ephemeral"
        )
