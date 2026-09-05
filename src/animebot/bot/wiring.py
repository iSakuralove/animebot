"""把 @command 声明的函数绑到 aiogram Router 上。

模块作者不碰 aiogram 的 Command 过滤器，只写 @command。这里统一：
  - 注册进 CommandRegistry（/help、setMyCommands 的数据源）
  - 生成 Command 过滤器（含别名）
  - 按 handler 签名注入依赖，不需要的参数不传
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from aiogram import Router
from aiogram.filters import Command

from ..core.feature import BaseFeature
from ..core.registry import CommandRegistry, CommandSpec, spec_of
from ..observability.logging import get_logger

log = get_logger("animebot.wiring")


def collect_specs(module: Any) -> list[CommandSpec]:
    """扫模块里所有带 @command 标记的函数。"""
    found: list[CommandSpec] = []
    for _name, obj in vars(module).items():
        if callable(obj) and (spec := spec_of(obj)) is not None:
            found.append(spec)
    return found


def bind_commands(
    router: Router,
    specs: Iterable[CommandSpec],
    registry: CommandRegistry,
    feature_name: str,
) -> None:
    for spec in specs:
        bound = spec.with_feature(feature_name)
        registry.add(bound)
        router.message.register(_adapt(spec.handler), Command(*bound.all_names))
        log.debug("command.bound", command=bound.qualname, aliases=bound.aliases)


def _adapt(fn: Any) -> Any:
    """按签名过滤 kwargs。

    aiogram 会把 data 里所有键当 kwargs 传进来，handler 只声明它要的参数。
    不做这层适配，每个 handler 都得写 **_kw，或者被无关参数炸掉。
    """
    sig = inspect.signature(fn)
    accepts_all = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    wanted = {
        name
        for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }

    async def wrapper(event: Any, **kw: Any) -> Any:
        if accepts_all:
            return await fn(event, **kw)
        return await fn(event, **{k: v for k, v in kw.items() if k in wanted})

    wrapper.__name__ = getattr(fn, "__name__", "handler")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def build_feature_router(
    feature: BaseFeature,
    handlers_module: Any,
    registry: CommandRegistry,
) -> Router:
    """标准模块装配：一个 Router + 扫描 handlers 模块里的 @command。"""
    router = Router(name=f"feature.{feature.name}")
    bind_commands(router, collect_specs(handlers_module), registry, feature.name)
    return router
