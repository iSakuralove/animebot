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

from ..config import Channel, LinkMode, Settings
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


async def check_channel_access(bot: Bot, channel: Channel) -> ChannelAccess:
    """查 bot 在目标频道的身份。任何异常都转成结果对象，不抛。

    启动自检不该能让 bot 起不来 —— 网络抖一下就拒绝启动是把可用性换成了洁癖。
    """
    chat_id = channel.bot_api_chat_id
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


async def preflight(bot: Bot, settings: Settings) -> dict[str, ChannelAccess]:
    """每个被索引的频道跑一遍自检，结论写进日志。返回值（频道 key -> 结论）供 /health 复用。"""
    out: dict[str, ChannelAccess] = {}
    for channel in settings.channels:
        out[channel.key] = access = await check_channel_access(bot, channel)
        _log_access(channel, access, settings.link_mode)
    return out


def _log_access(channel: Channel, access: ChannelAccess, link_mode: LinkMode) -> None:
    if not access.ok:
        log.warning("preflight.channel_unreachable", channel=channel.key,
                    channel_id=channel.channel_id, status=access.status, hint=access.hint)
        return
    log.info(
        "preflight.channel_ok",
        channel=channel.key,
        title=access.chat_title,
        username=access.username,
        status=access.status,
    )
    # 配的 username 和频道实际的不一致 -> 公开深链会指向错误的地方或 404
    configured = channel.username.lstrip("@").lower()
    actual = access.username.lower()
    if link_mode == "public" and actual and configured != actual:
        log.warning(
            "preflight.username_mismatch",
            channel=channel.key,
            configured=channel.username,
            actual=access.username,
            hint="公开深链会指向错误的频道或退回 /c/ 链接，改对应频道的 *CHANNEL_USERNAME",
        )
