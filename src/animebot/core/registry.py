"""指令注册表。

一个模块要加指令，只写：

    @command("bgm", desc="查 Bangumi 番剧信息", usage="/bgm 无职英雄", rate=(3, 10))
    async def cmd_bgm(msg: Message, ctx: CommandContext) -> None:
        ...

装饰器只登记元数据，不包裹逻辑 —— 计时、埋点、错误边界、限流全部在中间件里
统一做一次。装饰器里塞行为会导致：写新指令的人忘记加某个装饰器就少一层保护，
而且多层装饰器叠起来后栈追踪没法看。

/help 和 setMyCommands 都从这张表生成，不存在「加了指令忘记更新帮助」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from .errors import ConfigError

Handler = Callable[..., Awaitable[Any]]
H = TypeVar("H", bound=Handler)


@dataclass(frozen=True, slots=True)
class RateLimit:
    times: int          # 允许的次数
    per_seconds: float  # 时间窗

    @classmethod
    def of(cls, spec: tuple[int, float] | RateLimit | None) -> RateLimit | None:
        if spec is None or isinstance(spec, RateLimit):
            return spec
        return cls(times=spec[0], per_seconds=spec[1])


@dataclass(slots=True)
class CommandSpec:
    name: str                        # 不带斜杠
    handler: Handler
    feature: str = ""                # 归属模块，由 registry 填
    desc: str = ""                   # 一行说明，进 /help 和 Telegram 指令菜单
    usage: str = ""                   # 用法示例，参数错时展示
    aliases: tuple[str, ...] = ()
    admin_only: bool = False
    hidden: bool = False             # 不进 /help 和指令菜单
    rate: RateLimit | None = None
    group_allowed: bool = True       # 是否允许在群里用
    long_help: str = ""              # /help <cmd> 的详细说明

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def qualname(self) -> str:
        return f"{self.feature}.{self.name}" if self.feature else self.name

    def with_feature(self, feature: str) -> CommandSpec:
        """装配时打上归属模块。用 replace 而不是手抄字段 —— 加字段不会漏。"""
        return replace(self, feature=feature)


# 模块级收集器：装饰器在 import 期把 spec 挂到函数上，由 registry 扫描收集。
_ATTR = "__animebot_command__"


def command(
    name: str,
    *,
    desc: str = "",
    usage: str = "",
    aliases: tuple[str, ...] | str = (),
    admin_only: bool = False,
    hidden: bool = False,
    rate: tuple[int, float] | RateLimit | None = None,
    group_allowed: bool = True,
    long_help: str = "",
) -> Callable[[H], H]:
    """声明一个指令。只登记元数据，不包裹行为。"""
    if name.startswith("/"):
        name = name[1:]
    alias_tuple = (aliases,) if isinstance(aliases, str) else tuple(aliases)

    def deco(fn: H) -> H:
        setattr(
            fn,
            _ATTR,
            CommandSpec(
                name=name,
                handler=fn,
                desc=desc,
                usage=usage or f"/{name}",
                aliases=alias_tuple,
                admin_only=admin_only,
                hidden=hidden,
                rate=RateLimit.of(rate),
                group_allowed=group_allowed,
                long_help=long_help,
            ),
        )
        return fn

    return deco


def spec_of(fn: Any) -> CommandSpec | None:
    return getattr(fn, _ATTR, None)


class CommandRegistry:
    """所有指令的唯一真相来源。"""

    def __init__(self) -> None:
        self._by_name: dict[str, CommandSpec] = {}
        self._order: list[CommandSpec] = []

    def add(self, spec: CommandSpec) -> None:
        for n in spec.all_names:
            if n in self._by_name:
                other = self._by_name[n]
                raise ConfigError(
                    f"指令 /{n} 重复注册: {spec.qualname} 与 {other.qualname}"
                )
        for n in spec.all_names:
            self._by_name[n] = spec
        self._order.append(spec)

    def get(self, name: str) -> CommandSpec | None:
        """大小写敏感查找。/s 和 /S 是两条不同指令（/S 预留给特殊搜索）。

        aiogram 的 Command 过滤器默认也是大小写敏感（ignore_case=False），
        这里与之保持一致，否则 /help 能查到但真发指令时路由不到。
        """
        return self._by_name.get(name.lstrip("/"))

    def __iter__(self) -> Iterator[CommandSpec]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def visible(self) -> list[CommandSpec]:
        return [s for s in self._order if not s.hidden and not s.admin_only]

    def by_feature(self) -> dict[str, list[CommandSpec]]:
        out: dict[str, list[CommandSpec]] = {}
        for s in self._order:
            out.setdefault(s.feature or "misc", []).append(s)
        return out
