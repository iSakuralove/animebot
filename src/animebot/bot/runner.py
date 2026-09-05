"""bot 运行入口。`python -m animebot run` 会走到这里。"""

from __future__ import annotations

import asyncio
import contextlib

from ..observability.logging import get_logger
from .app import build_app, resolve_update_types, sync_bot_commands
from .preflight import preflight

log = get_logger("animebot.runner")


async def run_polling() -> None:
    app = await build_app()
    assert app.bot is not None and app.dp is not None
    try:
        await sync_bot_commands(app)
        me = await app.bot.get_me()
        log.info("bot.start", username=me.username, bot_id=me.id)
        # 频道自检：bot 不是管理员就收不到 channel_post，而那个失败是静默的。
        # 只警告不阻断 —— 网络抖一下就拒绝启动是把可用性换成了洁癖。
        app.channel_access = await preflight(app.bot, app.settings)
        await app.dp.start_polling(
            app.bot,
            handle_signals=False,
            allowed_updates=resolve_update_types(app.dp),
        )
    finally:
        log.info("bot.stopping")
        await app.aclose()


def main() -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_polling())
    return 0
