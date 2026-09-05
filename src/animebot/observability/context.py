"""贯穿一次请求的上下文。

用 contextvars 而不是到处传参数：一个 update 从中间件进来，经过 handler、
service、HTTP 客户端、SQL 层，中间要穿 5~6 层调用栈。让每层都多带一个
ctx 参数是纯粹的噪音，而且异步任务里一不小心就传丢了。

contextvars 在 asyncio 里天然按 Task 隔离，并发的两个 update 不会串味。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_trace: ContextVar[RequestContext | None] = ContextVar("animebot_trace", default=None)


def new_trace_id() -> str:
    """8 位足够：日志按天切分，同一天内碰撞概率可以忽略，短 id 方便用户复述。"""
    return uuid.uuid4().hex[:8]


@dataclass(slots=True)
class RequestContext:
    trace_id: str = field(default_factory=new_trace_id)
    update_id: int | None = None
    user_id: int | None = None
    username: str | None = None
    chat_id: int | None = None
    chat_type: str | None = None
    feature: str | None = None      # 哪个模块处理的
    command: str | None = None      # /search、/bgm ...
    payload: str | None = None      # 指令参数，截断后的
    started: float = field(default_factory=time.perf_counter)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def bind(self, **kw: Any) -> None:
        """handler 里补充信息，后续所有日志自动带上。"""
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.extra[k] = v

    def as_log_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"trace_id": self.trace_id}
        for k in ("update_id", "user_id", "username", "chat_id",
                  "chat_type", "feature", "command"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        out.update(self.extra)
        return out


def current() -> RequestContext | None:
    return _trace.get()


def trace_id() -> str:
    ctx = _trace.get()
    return ctx.trace_id if ctx else "-"


def bind(**kw: Any) -> None:
    """给当前上下文补字段。没有上下文时静默忽略（CLI / 测试场景）。"""
    if ctx := _trace.get():
        ctx.bind(**kw)


def set_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _trace.set(ctx)


def reset_context(token: Token[RequestContext | None]) -> None:
    _trace.reset(token)


@contextmanager
def request_context(**kw: Any) -> Iterator[RequestContext]:
    """CLI、定时任务、测试里手动开一个 trace。"""
    ctx = RequestContext(**kw)
    token = _trace.set(ctx)
    try:
        yield ctx
    finally:
        _trace.reset(token)
