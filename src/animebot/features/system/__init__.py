"""system 模块：/help、/ping、/metrics、/health、/trace。

它自己也是普通模块，没有特权 —— 用来验证模块协议够不够用。
"""

from __future__ import annotations

from aiogram import Router

from ...core.feature import BaseFeature
from . import handlers


class SystemFeature(BaseFeature):
    name = "system"
    requires = ("repo", "registry")

    def router(self) -> Router:
        from ...bot.wiring import build_feature_router

        return build_feature_router(self, handlers, self.container.get("registry"))


FEATURE = SystemFeature
