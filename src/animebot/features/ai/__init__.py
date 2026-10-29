"""ai 模块：`/q` AI 对话，带 Bangumi / 联网 / 频道检索三个工具。

模型走 Anthropic Messages 协议（网关兼容），流式输出靠 Telegram 的
`sendMessageDraft`。设计取舍见 [ADR-0016](../../../docs/ADR/0016-ai-chat-with-tools.md)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router

from ...core.feature import BaseFeature
from ...observability.logging import get_logger
from . import handlers
from .chat import ChatEngine
from .client import ClaudeClient
from .session import ConversationStore
from .tools import ToolBox

if TYPE_CHECKING:
    from ...config import Settings
    from ...core.container import Container

log = get_logger("animebot.ai.feature")


class AiFeature(BaseFeature):
    name = "ai"
    # query_store 不在依赖里：模块没加载时它也由 core 提供，与这里无关。
    requires = ("repo", "registry", "search")

    def __init__(self, settings: Settings, container: Container) -> None:
        super().__init__(settings, container)
        self._client = ClaudeClient(settings)
        self._toolbox: ToolBox | None = None

    async def setup(self) -> None:
        # 没有 key 也照样装配：handler 要能在用户敲 /q 时回「还没配好」，
        # 而不是因为缺依赖抛 TypeError 变成「内部错误」。
        configured = bool(self.settings.ai_api_key.get_secret_value())
        if configured:
            await self._client.open()
        else:
            log.warning(
                "ai.not_configured",
                hint="ANIMEBOT_AI_API_KEY 为空，/q 会提示未配置",
            )

        searches = {"anime": self.container.get("search")}
        if self.container.has("manga_search"):
            searches["manga"] = self.container.get("manga_search")
        self._toolbox = ToolBox(self.settings, searches=searches)

        self.container.put(
            "engine", ChatEngine(self.settings, self._client, self._toolbox)
        )
        self.container.put(
            "conversations",
            ConversationStore(ttl=self.settings.ai_session_ttl, max_keys=512),
        )
        log.info(
            "ai.ready" if configured else "ai.pending_key",
            model=self.settings.ai_model,
            fallback=self.settings.ai_fallback_model,
            base_url=self.settings.ai_base_url,
            tools=[t["name"] for t in ToolBox.specs()],
        )

    async def teardown(self) -> None:
        if self._toolbox is not None:
            await self._toolbox.close()
        await self._client.close()

    def router(self) -> Router:
        from ...bot.wiring import build_feature_router

        router = build_feature_router(self, handlers, self.container.get("registry"))
        handlers.register_callbacks(router)
        return router

    def health(self) -> dict[str, object]:
        configured = bool(self.settings.ai_api_key.get_secret_value())
        return {
            "status": "ok" if configured else "unconfigured",
            "model": self.settings.ai_model,
            "fallback": self.settings.ai_fallback_model,
            "max_tool_rounds": self.settings.ai_max_tool_rounds,
            "sessions": len(self.container.get("conversations")),
        }


FEATURE = AiFeature
