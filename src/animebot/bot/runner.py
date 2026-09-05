"""bot 运行入口。`python -m animebot run` 会走到这里。"""

from __future__ import annotations

import asyncio
import contextlib

from ..observability.logging import get_logger
from .app import build_app, sync_bot_commands

log = get_logger("animebot.runner")


async def run_polling() -> None:
    app = await build_app()
    assert app.bot is not None and app.dp is not None
    try:
        await sync_bot_commands(app)
        me = await app.bot.get_me()
        log.info("bot.start", username=me.username, bot_id=me.id)
        # 丢弃积压的 update：重启后不该把停机期间的历史指令全部重放一遍
        await app.dp.start_polling(app.bot, handle_signals=False,
                                   allowed_updates=app.dp.resolve_used_update_types())
    finally:
        log.info("bot.stopping")
        await app.aclose()


def main() -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_polling())
    return 0
