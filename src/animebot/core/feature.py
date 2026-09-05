"""功能模块协议。

一个模块 = 一个包 + 一个 Feature 子类。加 /bgm 就是：

    src/animebot/features/bgm/
        __init__.py     -> class BgmFeature(Feature)
        handlers.py     -> @command("bgm") 的函数
        client.py       -> Bangumi API 客户端

然后往 ANIMEBOT_FEATURES 里加 "bgm"。app 层不认识 bgm 这个词，
所以加模块永远不需要改 app.py —— 这是"扩展性"唯一有意义的落地形式。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from aiogram import Router

    from ..config import Settings
    from .container import Container


@runtime_checkable
class Feature(Protocol):
    """功能模块。名字唯一，指令通过 router 挂上去。"""

    name: str

    def router(self) -> Router:
        """返回本模块的 Router。指令用 @command 声明，由 wiring 自动绑定。"""
        ...


class BaseFeature:
    """默认实现，覆盖需要的钩子就行。"""

    name: str = ""
    #: 声明依赖的资源名（container 里的键），启动时校验，缺了就启动失败而不是运行时炸
    requires: tuple[str, ...] = ()

    def __init__(self, settings: Settings, container: Container) -> None:
        self.settings = settings
        self.container = container

    async def setup(self) -> None:
        """启动钩子：建连接、预热缓存。抛异常会让整个 bot 启动失败。"""

    async def teardown(self) -> None:
        """关停钩子：必须容忍 setup 没跑完就被调用。"""

    def router(self) -> Router:  # pragma: no cover - 子类必须实现
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        """给 /health 用。返回任意可 JSON 化的诊断信息。"""
        return {"status": "ok"}
