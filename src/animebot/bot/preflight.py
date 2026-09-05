"""启动自检：确认增量同步的前置条件真的成立。

为什么必须在启动时查：Bot API 的 `channel_post` **只推给频道的管理员 bot**。
bot 没加进频道、或者被降成普通成员，Telegram 就一条 update 都不推 ——
而这个失败是完全静默的：进程活着、指令能用、日志干净，只有索引悄悄停止更新。
等到几个月后有人问「怎么搜不到新番」才发现。

所以宁可启动时吵一次。
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

from ..config import Settings
from ..observability.logging import get_logger

log = get_logger("animebot.preflight")

# 能收到 channel_post 的身份。creator 是频道创建者（bot 不可能是），列上是为了完整。
_CAN_READ = frozenset({ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR})


@dataclass(frozen=True, slots=True)
class ChannelAccess:
    ok: bool
    status: str          # administrator / member / left / kicked / error
    detail: str = ""
    chat_title: str = ""
    username: str = ""

    @property
    def hint(self) -> str:
        """给人看的下一步动作。"""
        if self.ok:
            return ""
        if self.status == "error":
            return f"无法访问频道：{self.detail}"
        return (
            f"bot 在频道里的身份是 {self.status}，收不到 channel_post。"
            "把 bot 加进频道并设为管理员，增量同步才会工作。"
        )


async def check_channel_access(bot: Bot, settings: Settings) -> ChannelAccess:
    """查 bot 在目标频道的身份。任何异常都转成结果对象，不抛。

    启动自检不该能让 bot 起不来 —— 网络抖一下就拒绝启动是把可用性换成了洁癖。
    """
    chat_id = settings.bot_api_chat_id
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError as exc:
        return ChannelAccess(ok=False, status="error", detail=str(exc))

    status = str(member.status)
    return ChannelAccess(
        ok=member.status in _CAN_READ,
        status=status,
        chat_title=chat.title or "",
        username=chat.username or "",
    )


async def preflight(bot: Bot, settings: Settings) -> ChannelAccess:
    """跑一遍自检并把结论写进日志。返回结果供 /health 复用。"""
    access = await check_channel_access(bot, settings)
    if access.ok:
        log.info(
            "preflight.channel_ok",
            channel=access.chat_title,
            username=access.username,
            status=access.status,
        )
        # 配的 username 和频道实际的不一致 -> 深链会指向错误的地方或 404
        configured = settings.channel_username.lstrip("@").lower()
        actual = access.username.lower()
        if settings.link_mode == "public" and actual and configured != actual:
            log.warning(
                "preflight.username_mismatch",
                configured=settings.channel_username,
                actual=access.username,
                hint="公开深链会指向错误的频道，改 ANIMEBOT_CHANNEL_USERNAME",
            )
    else:
        log.warning("preflight.channel_unreachable", status=access.status,
                    hint=access.hint)
    return access
