"""内核测试：注册表、容器、错误分类、上下文、埋点。

这些是「业务以外」的部分，也是最容易在加模块时被悄悄破坏的部分。
"""

from __future__ import annotations

import asyncio

import pytest

from animebot.core.container import Container
from animebot.core.errors import (
    ConfigError,
    ExternalServiceError,
    NotFound,
    PermissionDenied,
    RateLimited,
    UsageError,
    UserError,
)
from animebot.core.registry import CommandRegistry, RateLimit, command, spec_of
from animebot.observability.context import (
    RequestContext,
    bind,
    current,
    request_context,
    trace_id,
)
from animebot.observability.metrics import Metrics


class TestCommandDecorator:
    def test_attaches_spec(self) -> None:
        @command("foo", desc="测试", rate=(3, 10))
        async def handler() -> None: ...

        spec = spec_of(handler)
        assert spec is not None
        assert spec.name == "foo"
        assert spec.desc == "测试"
        assert spec.rate == RateLimit(times=3, per_seconds=10)

    def test_strips_leading_slash(self) -> None:
        @command("/bar")
        async def handler() -> None: ...

        assert spec_of(handler).name == "bar"  # type: ignore[union-attr]

    def test_default_usage(self) -> None:
        @command("baz")
        async def handler() -> None: ...

        assert spec_of(handler).usage == "/baz"  # type: ignore[union-attr]

    def test_string_alias_normalized_to_tuple(self) -> None:
        @command("qux", aliases="q")
        async def handler() -> None: ...

        assert spec_of(handler).aliases == ("q",)  # type: ignore[union-attr]

    def test_does_not_wrap_function(self) -> None:
        """装饰器只贴元数据，不改变调用行为 —— 栈追踪才干净。"""

        @command("plain")
        async def handler() -> str:
            return "called"

        assert asyncio.run(handler()) == "called"


class TestRegistry:
    def _spec(self, name: str, **kw: object):
        @command(name, **kw)  # type: ignore[arg-type]
        async def h() -> None: ...

        s = spec_of(h)
        assert s is not None
        return s

    def test_lookup_by_name_and_alias(self) -> None:
        r = CommandRegistry()
        r.add(self._spec("search", aliases=("s", "find")))
        assert r.get("search") is r.get("s") is r.get("find")

    def test_lookup_tolerates_slash_but_is_case_sensitive(self) -> None:
        """去斜杠但大小写敏感：/s 和 /S 是两条不同指令（/S 预留给特殊搜索）。"""
        r = CommandRegistry()
        r.add(self._spec("s"))
        assert r.get("/s") is not None
        assert r.get("s") is not None
        assert r.get("/S") is None
        assert r.get("S") is None

    def test_duplicate_rejected_at_startup(self) -> None:
        """重名指令必须启动期就炸，而不是运行时随机命中一个。"""
        r = CommandRegistry()
        r.add(self._spec("dup"))
        with pytest.raises(ConfigError, match="重复注册"):
            r.add(self._spec("dup"))

    def test_alias_collision_rejected(self) -> None:
        r = CommandRegistry()
        r.add(self._spec("first", aliases=("x",)))
        with pytest.raises(ConfigError):
            r.add(self._spec("second", aliases=("x",)))

    def test_visible_excludes_hidden_and_admin(self) -> None:
        r = CommandRegistry()
        r.add(self._spec("open"))
        r.add(self._spec("secret", hidden=True))
        r.add(self._spec("root", admin_only=True))
        assert [s.name for s in r.visible()] == ["open"]

    def test_unknown_returns_none(self) -> None:
        assert CommandRegistry().get("nope") is None


class TestContainer:
    def test_put_and_get(self, settings) -> None:
        c = Container(settings)
        c.put("thing", 42)
        assert c.get("thing") == 42

    def test_duplicate_rejected(self, settings) -> None:
        c = Container(settings)
        c.put("thing", 1)
        with pytest.raises(ConfigError):
            c.put("thing", 2)

    def test_missing_lists_available(self, settings) -> None:
        c = Container(settings)
        c.put("repo", object())
        with pytest.raises(ConfigError, match="repo"):
            c.get("absent")

    def test_require_fails_fast(self, settings) -> None:
        """模块声明的依赖缺失，必须启动期报错而不是用户敲指令时才炸。"""
        c = Container(settings)
        c.put("repo", object())
        with pytest.raises(ConfigError, match="http"):
            c.require("repo", "http")

    async def test_close_reverse_order(self, settings) -> None:
        order: list[str] = []
        c = Container(settings)

        async def closer(name: str) -> None:
            order.append(name)

        c.put("a", 1, closer=lambda: closer("a"))
        c.put("b", 2, closer=lambda: closer("b"))
        await c.aclose()
        assert order == ["b", "a"]

    async def test_one_bad_closer_does_not_block_others(self, settings) -> None:
        closed: list[str] = []
        c = Container(settings)

        async def ok() -> None:
            closed.append("ok")

        async def boom() -> None:
            raise RuntimeError("连接已经断了")

        c.put("ok", 1, closer=ok)
        c.put("bad", 2, closer=boom)
        with pytest.raises(RuntimeError, match="关停时出错"):
            await c.aclose()
        assert closed == ["ok"]   # 坏的没拖累好的


class TestErrors:
    def test_user_error_message_is_shown(self) -> None:
        assert UserError("参数不对").user_message == "参数不对"

    def test_usage_error_appends_usage(self) -> None:
        e = UsageError("要搜什么？", "/search 无职英雄")
        assert "/search 无职英雄" in e.user_message

    def test_not_found(self) -> None:
        assert NotFound("这部番").user_message == "没找到 这部番"

    def test_rate_limited_carries_retry(self) -> None:
        e = RateLimited(12.4)
        assert e.retry_after == 12.4
        assert "12" in e.user_message

    def test_external_error_hides_detail_from_user(self) -> None:
        """外部服务的错误细节进日志，不进用户视野。"""
        e = ExternalServiceError("Bangumi", "HTTP 503 upstream timeout")
        assert "503" in str(e)
        assert "503" not in e.user_message
        assert e.service == "Bangumi"

    def test_permission_denied_is_user_error(self) -> None:
        assert isinstance(PermissionDenied(), UserError)


class TestRequestContext:
    def test_no_context_outside_scope(self) -> None:
        assert current() is None
        assert trace_id() == "-"

    def test_context_manager_scopes(self) -> None:
        with request_context(command="search") as ctx:
            assert current() is ctx
            assert trace_id() == ctx.trace_id
        assert current() is None

    def test_bind_known_and_unknown_fields(self) -> None:
        with request_context() as ctx:
            bind(user_id=7, query="无职英雄")
            assert ctx.user_id == 7
            assert ctx.extra["query"] == "无职英雄"

    def test_bind_outside_context_is_noop(self) -> None:
        bind(user_id=1)   # 不该抛

    def test_log_fields_skip_none(self) -> None:
        ctx = RequestContext(user_id=5, command="ping")
        fields = ctx.as_log_fields()
        assert fields["user_id"] == 5
        assert "chat_id" not in fields
        assert fields["trace_id"] == ctx.trace_id

    def test_trace_ids_unique(self) -> None:
        ids = {RequestContext().trace_id for _ in range(200)}
        assert len(ids) == 200

    async def test_isolated_across_tasks(self) -> None:
        """并发的两个 update 不能串 trace —— contextvars 按 Task 隔离。"""
        seen: list[str] = []

        async def worker(name: str) -> None:
            with request_context(command=name) as ctx:
                await asyncio.sleep(0.01)
                assert current() is ctx
                assert ctx.command == name
                seen.append(ctx.trace_id)

        await asyncio.gather(worker("a"), worker("b"), worker("c"))
        assert len(set(seen)) == 3


class TestMetrics:
    def test_counter(self) -> None:
        m = Metrics()
        m.incr("hits")
        m.incr("hits", 3)
        assert m.snapshot()["counters"]["hits"] == 4

    def test_labels_separate_series(self) -> None:
        m = Metrics()
        m.incr("calls", command="search")
        m.incr("calls", command="bgm")
        counters = m.snapshot()["counters"]
        assert counters["calls{command=search}"] == 1
        assert counters["calls{command=bgm}"] == 1

    def test_histogram_stats(self) -> None:
        m = Metrics()
        for v in (10, 20, 30, 1000):
            m.observe("lat", v)
        stats = m.snapshot()["timings"]["lat"]
        assert stats["count"] == 4
        assert stats["max_ms"] == 1000
        assert 0 < stats["avg_ms"] < 1000

    def test_quantile_between_min_and_max(self) -> None:
        m = Metrics()
        for v in range(1, 101):
            m.observe("lat", float(v))
        stats = m.snapshot()["timings"]["lat"]
        assert stats["p50_ms"] <= stats["p95_ms"] <= stats["max_ms"]

    def test_timer_records(self) -> None:
        m = Metrics()
        with m.timer("work"):
            pass
        assert m.snapshot()["timings"]["work"]["count"] == 1

    def test_reset(self) -> None:
        m = Metrics()
        m.incr("x")
        m.reset()
        assert m.snapshot()["counters"] == {}
