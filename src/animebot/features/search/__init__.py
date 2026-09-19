"""search 模块：/search、/check、/tags，以及结果按钮的回调。"""

from __future__ import annotations

from aiogram import Router

from ...core.feature import BaseFeature
from . import handlers


class SearchFeature(BaseFeature):
    name = "search"
    requires = ("repo", "search", "registry", "query_store")

    def router(self) -> Router:
        from ...bot.wiring import build_feature_router

        router = build_feature_router(self, handlers, self.container.get("registry"))
        handlers.register_callbacks(router)
        handlers.register_plain_search(router)
        return router

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "db": str(self.settings.db_path),
            "query_cache": len(self.container.get("query_store")),
        }


FEATURE = SearchFeature
