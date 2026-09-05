"""HttpClient 测试。用本地 aiohttp 测试服务器，不出网。

重点盯：重试只对该重试的状态码生效、缓存真的少发请求、
外部错误细节不泄给用户。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from animebot.core.errors import ExternalServiceError
from animebot.core.http import HttpClient
from animebot.observability.metrics import METRICS


class Counter:
    def __init__(self) -> None:
        self.n = 0


@pytest.fixture
async def server() -> AsyncIterator[tuple[str, Counter, dict]]:
    """一个可编程的假上游：behavior 决定它怎么回。"""
    hits = Counter()
    behavior: dict = {"mode": "ok"}

    async def handler(request: web.Request) -> web.Response:
        hits.n += 1
        mode = behavior["mode"]
        if mode == "ok":
            return web.json_response({"ok": True, "n": hits.n})
        if mode == "flaky":       # 前两次 503，第三次成功
            if hits.n < 3:
                return web.json_response({"err": "busy"}, status=503)
            return web.json_response({"ok": True, "n": hits.n})
        if mode == "500":
            return web.json_response({"err": "boom"}, status=500)
        if mode == "404":
            return web.json_response({"err": "no such subject"}, status=404)
        if mode == "text":        # 上游 content-type 不对也要能解析
            return web.Response(text='{"ok": true}', content_type="text/plain")
        raise AssertionError(mode)

    app = web.Application()
    app.router.add_get("/probe", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}", hits, behavior
    finally:
        await runner.cleanup()


class TestHttpClient:
    async def test_plain_get(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url=base, cache_ttl=0) as c:
            data = await c.get_json("/probe")
        assert data == {"ok": True, "n": 1}
        assert hits.n == 1

    async def test_cache_avoids_second_call(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url=base, cache_ttl=60) as c:
            first = await c.get_json("/probe")
            second = await c.get_json("/probe")
        assert first == second
        assert hits.n == 1, "缓存没生效，又出网了一次"

    async def test_cache_key_includes_params(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url=base, cache_ttl=60) as c:
            await c.get_json("/probe", params={"q": "a"})
            await c.get_json("/probe", params={"q": "b"})
        assert hits.n == 2, "不同参数被错误地共享了缓存"

    async def test_cache_can_be_bypassed(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url=base, cache_ttl=60) as c:
            await c.get_json("/probe")
            await c.get_json("/probe", use_cache=False)
        assert hits.n == 2

    async def test_retries_5xx_then_succeeds(self, server) -> None:
        base, hits, behavior = server
        behavior["mode"] = "flaky"
        async with HttpClient("Fake", base_url=base, retries=3, cache_ttl=0) as c:
            data = await c.get_json("/probe")
        assert data["ok"] is True
        assert hits.n == 3, "没有重试到成功"

    async def test_retries_exhausted_raises(self, server) -> None:
        base, hits, behavior = server
        behavior["mode"] = "500"
        async with HttpClient("Fake", base_url=base, retries=1, cache_ttl=0) as c:
            with pytest.raises(ExternalServiceError):
                await c.get_json("/probe")
        assert hits.n == 2, "重试次数不对"

    async def test_4xx_not_retried(self, server) -> None:
        """404 重试是浪费 —— 上游明确说没有。"""
        base, hits, behavior = server
        behavior["mode"] = "404"
        async with HttpClient("Fake", base_url=base, retries=3, cache_ttl=0) as c:
            with pytest.raises(ExternalServiceError):
                await c.get_json("/probe")
        assert hits.n == 1

    async def test_error_detail_hidden_from_user(self, server) -> None:
        base, _, behavior = server
        behavior["mode"] = "404"
        async with HttpClient("Bangumi", base_url=base, retries=0, cache_ttl=0) as c:
            with pytest.raises(ExternalServiceError) as ei:
                await c.get_json("/probe")
        assert "no such subject" in str(ei.value)
        assert "no such subject" not in ei.value.user_message
        assert "Bangumi" in ei.value.user_message

    async def test_wrong_content_type_still_parsed(self, server) -> None:
        base, _, behavior = server
        behavior["mode"] = "text"
        async with HttpClient("Fake", base_url=base, cache_ttl=0) as c:
            assert await c.get_json("/probe") == {"ok": True}

    async def test_connection_refused_becomes_external_error(self) -> None:
        async with HttpClient(
            "Dead", base_url="http://127.0.0.1:1", retries=0, cache_ttl=0
        ) as c:
            with pytest.raises(ExternalServiceError) as ei:
                await c.get_json("/nope")
        assert ei.value.service == "Dead"

    async def test_metrics_recorded(self, server) -> None:
        base, _, _ = server
        METRICS.reset()
        async with HttpClient("Fake", base_url=base, cache_ttl=60) as c:
            await c.get_json("/probe")
            await c.get_json("/probe")     # 缓存命中
        snap = METRICS.snapshot()
        assert any("service=Fake" in k and "http.calls" in k for k in snap["counters"])
        assert snap["counters"].get("http.cache_hit{service=Fake}") == 1
        assert any("http.latency" in k for k in snap["timings"])

    async def test_clear_cache(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url=base, cache_ttl=60) as c:
            await c.get_json("/probe")
            assert c.cache_size() == 1
            c.clear_cache()
            assert c.cache_size() == 0
            await c.get_json("/probe")
        assert hits.n == 2

    async def test_absolute_url_bypasses_base(self, server) -> None:
        base, hits, _ = server
        async with HttpClient("Fake", base_url="http://unused.invalid", cache_ttl=0) as c:
            await c.get_json(f"{base}/probe")
        assert hits.n == 1

    async def test_reopen_after_close(self, server) -> None:
        base, _, _ = server
        c = HttpClient("Fake", base_url=base, cache_ttl=0)
        await c.open()
        await c.close()
        await c.get_json("/probe")   # 应该自动重开
        await c.close()
