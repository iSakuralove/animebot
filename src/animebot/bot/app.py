"""应用组装。启动顺序、中间件顺序、模块加载、关停，都只在这里。"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from ..config import Settings, get_settings
from ..core.container import Container
from ..core.errors import ConfigError
from ..core.feature import BaseFeature
from ..core.registry import CommandRegistry
from ..ingest.sync import ChannelSync
from ..observability.logging import get_logger, setup_logging
from ..search.callbacks import QueryStore
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
    #: 启动自检的结论（频道 key -> 身份），由 runner 填。/health 读它回答
    #: 「各频道的增量同步在工作吗」。
    channel_access: dict[str, ChannelAccess] = field(default_factory=dict)

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
    # 超长查询词的暂存。有界 LRU，丢了只会让翻页提示"搜索已过期"。
    c.put("query_store", QueryStore(capacity=settings.query_store_size))
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

        # 直连被墙时走 http 代理。aiohttp 默认不读系统代理，必须显式传。
        session = AiohttpSession(proxy=settings.proxy) if settings.proxy else None
        if session is not None:
            log.info("bot.proxy", proxy=settings.proxy)
        app.bot = Bot(
            token=token,
            session=session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        app.dp = _build_dispatcher(settings, registry, container, app)
        for r in routers:
            app.dp.include_router(r)
        log.info("app.ready", **await _inventory(container.get("repo"), settings))
        return app
    except BaseException:
        await app.aclose()
        raise


async def _inventory(repo: PostRepo, settings: Settings) -> dict[str, dict[str, int]]:
    """每个频道库里有多少帖子 / 文件。启动日志里一眼看出「漫画频道还没回填」。"""
    posts: dict[str, int] = {}
    files: dict[str, int] = {}
    for c in settings.channels:
        posts[c.key] = await repo.count(channel_id=c.channel_id)
        files[c.key] = await repo.file_count(channel_id=c.channel_id)
    return {"posts": posts, "files": files}


def _build_dispatcher(
    settings: Settings,
    registry: CommandRegistry,
    container: Container,
    app: App,
) -> Dispatcher:
    dp = Dispatcher()
    # 依赖注入：handler 声明什么参数名就拿到什么。容器里的一切原样交出去 ——
    # 包括模块 setup() 时自己放进去的（如 manga_search），加模块不用改这里。
    for name, obj in container.items():
        dp[name] = obj
    dp["settings"] = settings
    dp["registry"] = registry
    dp["container"] = container
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


def _menu_items(
    registry: CommandRegistry, *, for_admin: bool, ephemeral: bool = False
) -> list[BotCommand]:
    """指令菜单条目。成员看 visible()；管理员额外看非隐藏的 admin_only 指令。

    hidden 的（/trace）对谁都不进菜单 —— 输入框自动补全里也不该有调试指令。

    `ephemeral=True` 是群聊 scope：用户在菜单里选出来的那条消息对其他人不可见
    （Bot API 10.2 的双向隐私）。**逐条可控** —— `CommandSpec.group_ephemeral`
    关掉就不标。检索类该开（查询词是私事），答案公开的 `/q` 必须关：否则群里
    看得到答案却看不到问题。
    """
    return [
        BotCommand(
            command=s.name,
            description=s.desc or s.name,
            is_ephemeral=True if (ephemeral and s.group_ephemeral) else None,
        )
        for s in registry
        if not s.hidden and (for_admin or not s.admin_only)
    ]


async def sync_bot_commands(app: App) -> None:
    """把注册表推到 Telegram 的指令菜单。加指令不需要手动去 BotFather 改。

    三层 scope：
      - 默认 scope：私聊里全员可见的成员指令。
      - 所有群聊：同一批指令，但标成 ephemeral —— 群里搜完不刷屏。
      - 每个管理员私聊：额外的 admin_only 指令（BotCommandScopeChat 只影响
        那个私聊），否则管理员也看不见自己的指令，只能手打。管理员还没跟 bot
        说过话时 chat 不存在，set 会 400：跳过打个 warning，等下次重启再补。
    """
    if app.bot is None:
        return
    member_menu = _menu_items(app.registry, for_admin=False)
    await app.bot.set_my_commands(member_menu, scope=BotCommandScopeDefault())
    group_menu = _menu_items(app.registry, for_admin=False, ephemeral=True)
    await app.bot.set_my_commands(group_menu, scope=BotCommandScopeAllGroupChats())
    pushed = 2
    if app.settings.admin_ids:
        admin_menu = _menu_items(app.registry, for_admin=True)
        for admin_id in app.settings.admin_ids:
            try:
                await app.bot.set_my_commands(
                    admin_menu, scope=BotCommandScopeChat(chat_id=admin_id)
                )
                pushed += 1
            except TelegramAPIError as exc:
                log.warning("bot.admin_menu_failed", admin_id=admin_id,
                            detail=str(exc)[:120])
    log.info("bot.commands_synced", member=len(member_menu), menus=pushed)


# 增量同步靠中间件而不是 handler（理由见 ChannelSyncMiddleware 的 docstring），
# 而 resolve_used_update_types() 只扫 handler —— 它会漏掉 channel_post，
# Telegram 就再也不推频道帖了，同步永久静默失效。所以显式补上。
_SYNC_UPDATES = ("channel_post", "edited_channel_post")


def resolve_update_types(dp: Dispatcher) -> list[str]:
    return sorted(set(dp.resolve_used_update_types()) | set(_SYNC_UPDATES))
