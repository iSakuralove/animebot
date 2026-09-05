"""外部 HTTP 客户端基类。

/bgm 查 Bangumi、/agent 调 LLM，凡是出网的都从这里继承。放在这一层的原因：
超时、重试、缓存、埋点、错误翻译，每个外部服务都要，而每次重写都会漏一样。

自动获得：
  - 超时和有限重试（指数退避 + 抖动，只重试 5xx/429/网络错误）
  - 按 (method, url, params) 的进程内 TTL 缓存
  - 每次调用的耗时/结果埋点，标签是服务名
  - 异常统一翻译成 ExternalServiceError（用户看到的是「暂时用不了」，
    细节只进日志）
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from typing import Any, Self

import aiohttp
import orjson

from ..core.errors import ExternalServiceError
from ..observability.logging import get_logger
from ..observability.metrics import METRICS

log = get_logger("animebot.http")

_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class _TTLCache:
    """进程内缓存。番剧元数据一天内不会变，没必要每次都出网。"""

    def __init__(self, ttl: float, max_items: int = 512) -> None:
        self._ttl = ttl
        self._max = max_items
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:
            # 简单粗暴：满了就丢最早过期的一半。LRU 在这个量级上不值得。
            for k in sorted(self._data, key=lambda k: self._data[k][0])[: self._max // 2]:
                del self._data[k]
        self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def _cache_key(method: str, url: str, params: dict[str, Any] | None) -> str:
    raw = f"{method}|{url}|{orjson.dumps(params or {}, option=orjson.OPT_SORT_KEYS).decode()}"
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


class HttpClient:
    """一个外部服务一个实例。service 名字会进日志、埋点和用户可见的错误里。"""

    def __init__(
        self,
        service: str,
        *,
        base_url: str = "",
        timeout: float = 10.0,
        retries: int = 2,
        cache_ttl: float = 900.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.service = service
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._headers = headers or {}
        self._cache = _TTLCache(cache_ttl)
        self._session: aiohttp.ClientSession | None = None

    # ---------------------------------------------------------- 生命周期
    async def open(self) -> Self:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers=self._headers
            )
        return self

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ---------------------------------------------------------- 请求
    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        key = _cache_key("GET", url, params)

        if use_cache and (cached := self._cache.get(key)) is not None:
            METRICS.incr("http.cache_hit", service=self.service)
            log.debug("http.cache_hit", service=self.service, url=url)
            return cached

        data = await self._request_with_retry(url, params)
        if use_cache:
            self._cache.put(key, data)
        return data

    async def _request_with_retry(self, url: str, params: dict[str, Any] | None) -> Any:
        session = (await self.open())._session
        assert session is not None
        last: Exception | None = None

        for attempt in range(self._retries + 1):
            t0 = time.perf_counter()
            try:
                async with session.get(url, params=params) as resp:
                    ms = (time.perf_counter() - t0) * 1000
                    METRICS.observe("http.latency", ms, service=self.service)
                    METRICS.incr("http.calls", service=self.service, status=resp.status)

                    if resp.status in _RETRY_STATUS and attempt < self._retries:
                        log.warning(
                            "http.retryable",
                            service=self.service,
                            status=resp.status,
                            attempt=attempt + 1,
                        )
                        last = ExternalServiceError(self.service, f"HTTP {resp.status}")
                        await self._backoff(attempt, resp.headers.get("Retry-After"))
                        continue

                    if resp.status >= 400:
                        body = (await resp.text())[:200]
                        raise ExternalServiceError(
                            self.service, f"HTTP {resp.status}: {body}"
                        )

                    log.info(
                        "http.ok", service=self.service, url=url,
                        status=resp.status, elapsed_ms=round(ms, 1),
                    )
                    return await resp.json(content_type=None)

            except TimeoutError as exc:
                last = ExternalServiceError(self.service, "超时")
                METRICS.incr("http.timeout", service=self.service)
                log.warning("http.timeout", service=self.service, attempt=attempt + 1)
                if attempt >= self._retries:
                    raise last from exc
                await self._backoff(attempt, None)

            except aiohttp.ClientError as exc:
                last = ExternalServiceError(self.service, repr(exc))
                METRICS.incr("http.client_error", service=self.service)
                log.warning("http.client_error", service=self.service, error=repr(exc))
                if attempt >= self._retries:
                    raise last from exc
                await self._backoff(attempt, None)

        raise last or ExternalServiceError(self.service, "重试耗尽")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """指数退避 + 抖动。抖动是为了避免多个请求同时醒来又一起打过去。"""
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), 30.0)
        else:
            delay = min(0.5 * 2**attempt, 8.0) * (0.5 + random.random())
        await asyncio.sleep(delay)

    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()
