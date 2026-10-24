"""manga 模块：/c 搜漫画频道，以及结果翻页、落地页标签按钮的回调。

漫画频道和动漫频道共用库、解析器、同步（频道是数据，见 config.Channel）；
这个模块只带来一个绑在漫画频道上的 SearchService 和自己的展示层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router

from ...core.feature import BaseFeature
from ...observability.logging import get_logger
from ...search.service import SearchService
from . import handlers

if TYPE_CHECKING:
    from ...config import Settings
    from ...core.container import Container

log = get_logger("animebot.manga")


class MangaFeature(BaseFeature):
    name = "manga"
    requires = ("repo", "registry", "query_store")

    def __init__(self, settings: Settings, container: Container) -> None:
        super().__init__(settings, container)
        self._posts = 0
        self._files = 0

    async def setup(self) -> None:
        repo = self.container.get("repo")
        channel = self.settings.manga
        self.container.put(
            "manga_search", SearchService(repo, self.settings, channel=channel)
        )
        self._posts = await repo.count(channel_id=channel.channel_id)
        self._files = await repo.file_count(channel_id=channel.channel_id)
        log.info(
            "manga.ready",
            channel_id=channel.channel_id,
            posts=self._posts,
            files=self._files,
        )
        if not self._posts:
            # /c 会对什么都回「没找到」—— 启动时就说清楚为什么，而不是等用户问。
            log.warning(
                "manga.empty",
                channel_id=channel.channel_id,
                hint="漫画频道还没回填：uv run animebot ingest <漫画频道导出目录>",
            )

    def router(self) -> Router:
        from ...bot.wiring import build_feature_router

        router = build_feature_router(self, handlers, self.container.get("registry"))
        handlers.register_callbacks(router)
        return router

    def health(self) -> dict[str, object]:
        return {
            "status": "ok" if self._posts else "empty",
            "channel": self.settings.manga.channel_id,
            "posts_at_boot": self._posts,
            "files_at_boot": self._files,
        }


FEATURE = MangaFeature
