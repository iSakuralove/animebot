"""应用组装。启动顺序、中间件顺序、模块加载、关停，都只在这里。"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from ..config import Settings, get_settings
from ..core.container import Container
from ..core.errors import ConfigError
from ..core.feature import BaseFeature
from ..core.registry import CommandRegistry
from ..ingest.sync import ChannelSync
from ..observability.logging import get_logger, setup_logging
from ..search.service import SearchService
from ..storage.repo import PostRepo
from .loader import load_features
from .middlewares import (
    AccessMiddleware,
    ChannelSyncMiddleware,
    ErrorBoundary,
    ObservabilityMiddleware,
    RateLimitMiddleware,
    TraceMiddleware,
)
from .preflight import ChannelAccess

log = get_logger("animebot.app")


@dataclass(slots=True)
class App:
    settings: Settings
    container: Container
    registry: CommandRegistry
    features: list[BaseFeature] = field(default_factory=list)
    bot: Bot | None = None
    dp: Dispatcher | None = None
    #: 启动自检的结论，由 runner 填。/health 读它回答「增量同步在工作吗」。
    channel_access: ChannelAccess | None = None

    async def aclose(self) -> None:
        for f in reversed(self.features):
            try:
                await f.teardown()
            except Exception:
                log.exception("feature.teardown_failed", feature=f.name)
        await self.container.aclose()
        if self.bot is not None:
            await self.bot.session.close()


async def build_container(settings: Settings) -> Container:
    """共享资源在这里建，模块通过名字取，不自己 new。"""
    c = Container(settings)
    repo = await PostRepo(settings.db_path).open()
    await repo.init_schema()
    c.put("repo", repo, closer=repo.close)
    c.put("search", SearchService(repo, settings))
    c.put("sync", ChannelSync(repo, settings))
    return c


async def build_app(settings: Settings | None = None, *, with_bot: bool = True) -> App:
    settings = settings or get_settings()
    setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        json_console=settings.log_json,
    )

    container = await build_container(settings)
    registry = CommandRegistry()
    container.put("registry", registry)
    app = App(settings=settings, container=container, registry=registry)

    try:
        app.features = load_features(settings, container)
        for f in app.features:
            await f.setup()
        # 先建 router：@command 是在这一步才登记进 registry 的。
        # 所以哪怕不起 bot（CLI / 测试），也要走一遍，否则 registry 是空的。
        routers = [f.router() for f in app.features]
        log.info(
            "app.wired",
            features=[f.name for f in app.features],
            commands=len(registry),
        )

        if not with_bot:
            return app

        token = settings.bot_token.get_secret_value()
        if not token:
            raise ConfigError(
                "缺少 bot token。在 .env 里设 AnimeCoffeebot=<token>"
            )

        app.bot = Bot(
            token=token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        app.dp = _build_dispatcher(settings, registry, container, app)
        for r in routers:
            app.dp.include_router(r)
        log.info("app.ready", posts=await container.get("repo").count())
        return app
    except BaseException:
        await app.aclose()
        raise


def _build_dispatcher(
    settings: Settings,
    registry: CommandRegistry,
    container: Container,
    app: App,
) -> Dispatcher:
    dp = Dispatcher()
    # 依赖注入：handler 声明什么参数名就拿到什么
    dp["settings"] = settings
    dp["registry"] = registry
    dp["container"] = container
    dp["repo"] = container.get("repo")
    dp["search"] = container.get("search")
    dp["sync"] = container.get("sync")
    dp["app"] = app

    # 顺序有意义：
    #   Trace 最外 —— 后面所有日志都要 trace_id。
    #   ErrorBoundary 次之 —— 它下游的一切异常都被兜住，包括限流和权限。
    #   Access / RateLimit 抛的 UserError 要能被 ErrorBoundary 抓到，所以在它内侧。
    #   Observability 在它们内侧 —— 只计时真正的业务处理，不含中间件开销。
    #   ChannelSync 最内 —— 频道帖不该经过限流/权限（那是给用户指令的），
    #     但要被计时和埋点，所以放在 Observability 内侧。
    for mw in (
        TraceMiddleware(registry),
        ErrorBoundary(),
        AccessMiddleware(registry, settings.admin_ids),
        RateLimitMiddleware(registry),
        ObservabilityMiddleware(settings),
        ChannelSyncMiddleware(container.get("sync")),
    ):
        dp.update.outer_middleware(mw)
    return dp


async def sync_bot_commands(app: App) -> None:
    """把注册表推到 Telegram 的指令菜单。加指令不需要手动去 BotFather 改。"""
    if app.bot is None:
        return
    cmds = [
        BotCommand(command=s.name, description=s.desc or s.name)
        for s in app.registry.visible()
    ]
    if cmds:
        await app.bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
        log.info("bot.commands_synced", count=len(cmds))


# 增量同步靠中间件而不是 handler（理由见 ChannelSyncMiddleware 的 docstring），
# 而 resolve_used_update_types() 只扫 handler —— 它会漏掉 channel_post，
# Telegram 就再也不推频道帖了，同步永久静默失效。所以显式补上。
_SYNC_UPDATES = ("channel_post", "edited_channel_post")


def resolve_update_types(dp: Dispatcher) -> list[str]:
    return sorted(set(dp.resolve_used_update_types()) | set(_SYNC_UPDATES))
