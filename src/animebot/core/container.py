"""依赖容器。

不引入 DI 框架。就是一个带生命周期的字典 —— 存 repo、http 客户端、
各种 service，模块通过名字取。

为什么不直接用全局变量：测试里要换成假的。为什么不用 DI 框架：
这个规模下框架的启动顺序魔法比手写 20 行更难调试。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..config import Settings
from .errors import ConfigError

T = TypeVar("T")

Closer = Callable[[], Awaitable[None]]


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._items: dict[str, Any] = {}
        self._closers: list[tuple[str, Closer]] = []

    def put(self, name: str, obj: T, *, closer: Closer | None = None) -> T:
        if name in self._items:
            raise ConfigError(f"容器里已存在 {name!r}")
        self._items[name] = obj
        if closer is not None:
            self._closers.append((name, closer))
        return obj

    def get(self, name: str) -> Any:
        try:
            return self._items[name]
        except KeyError:
            raise ConfigError(
                f"依赖 {name!r} 不存在。已注册: {sorted(self._items)}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._items

    def require(self, *names: str) -> None:
        """启动期校验。缺依赖要在启动时炸，不能等到用户敲指令。"""
        missing = [n for n in names if n not in self._items]
        if missing:
            raise ConfigError(
                f"缺少依赖 {missing}。已注册: {sorted(self._items)}"
            )

    async def aclose(self) -> None:
        """逆序关闭。单个失败不影响其它，避免一个坏连接卡死整个关停。"""
        errors: list[tuple[str, BaseException]] = []
        for name, closer in reversed(self._closers):
            try:
                await closer()
            except BaseException as exc:
                errors.append((name, exc))
        self._items.clear()
        self._closers.clear()
        if errors:
            detail = ", ".join(f"{n}: {e!r}" for n, e in errors)
            raise RuntimeError(f"关停时出错: {detail}")
