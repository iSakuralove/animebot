"""频道帖增量同步。

回填灌进来的是导出那一刻的快照。频道还在发新帖，而且**每一条帖子都会被
编辑** —— 真实数据里 1486/1486 有 edited 字段，作者会在链接失效后回去改。
只索引新帖不管编辑，等于索引里永远存着第一版内容。

所以 channel_post 和 edited_channel_post 走完全相同的路径：upsert 幂等，
不需要区分。

所有配置的频道（动漫 / 漫画）共用这一个实例：频道是数据，按 chat.id 查表，
不是每个频道一个同步服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.types import Message

from ..config import Channel, Settings
from ..domain.files import ChannelFile
from ..domain.post import ParseStatus, Post
from ..observability.logging import get_logger
from ..observability.metrics import METRICS
from ..parsing.files import parse_file
from ..parsing.post_parser import parse_message
from ..storage.repo import PostRepo
from .update_adapter import message_to_export_shape

log = get_logger("animebot.sync")


@dataclass(frozen=True, slots=True)
class SyncResult:
    """同步一条消息的结果。skipped 的原因要能区分，否则排查时只知道"没入库"。"""

    action: str          # upserted | attached | skipped
    reason: str          # ok | file | wrong_chat | not_post | empty | parse_failed
    post: Post | None = None
    file: ChannelFile | None = None

    @property
    def stored(self) -> bool:
        """帖子入库了没有。文件（attached）不算 —— 水位的 last_stored 只跟帖子走。"""
        return self.action == "upserted"


class ChannelSync:
    """频道帖 -> 索引库。与回填共用 parse_message / parse_file 和 upsert。"""

    def __init__(self, repo: PostRepo, settings: Settings) -> None:
        self._repo = repo
        self._cfg = settings
        self._by_chat: dict[int, Channel] = {c.bot_api_chat_id: c for c in settings.channels}

    def channel_of(self, msg: Message) -> Channel | None:
        """这条消息来自哪个被索引的频道。不是就返回 None。

        讨论群里的同名帖子是频道帖的自动转发副本，链接已失效（帖子正文自己就
        写着「讨论中的链接是失效的」）。灌进来会让搜索结果指向死链。这跟回填
        那道 channel_id 校验是同一个理由。
        """
        return self._by_chat.get(msg.chat.id)

    def accepts(self, msg: Message) -> bool:
        return self.channel_of(msg) is not None

    async def handle(self, msg: Message, *, kind: str = "new") -> SyncResult:
        channel = self.channel_of(msg)
        METRICS.incr("sync.received", channel=channel.key if channel else None, kind=kind)

        if channel is None:
            METRICS.incr("sync.skipped", reason="wrong_chat")
            log.debug("sync.wrong_chat", chat_id=msg.chat.id, message_id=msg.message_id)
            return SyncResult("skipped", "wrong_chat")

        shape = message_to_export_shape(msg)
        with METRICS.timer("sync.latency", channel=channel.key, kind=kind):
            post = parse_message(shape, channel.channel_id)
            file = parse_file(shape, channel.channel_id)

        if file is not None:
            await self._repo.upsert_files([file])
            METRICS.incr("sync.file", channel=channel.key, kind=kind)
            log.info(
                "sync.file",
                channel=channel.key,
                kind=kind,
                message_id=file.message_id,
                file_name=file.file_name[:80],
                file_size=file.file_size,
            )

        if post is None:
            # 没有正文：媒体组的第 2~n 张图、或者一个纯文件。都是正常情况，不是错误。
            # 仍要记水位：它证明 update 还在到达。
            await self._mark(channel, msg.message_id, stored=False)
            if file is not None:
                return SyncResult("attached", "file", file=file)
            METRICS.incr("sync.skipped", channel=channel.key, reason="empty")
            return SyncResult("skipped", "empty")

        if post.parse_status is ParseStatus.SKIPPED:
            await self._mark(channel, post.message_id, stored=False)
            METRICS.incr("sync.skipped", channel=channel.key, reason="not_post")
            log.info("sync.not_post", channel=channel.key, kind=kind,
                     message_id=post.message_id, text_head=post.raw_text[:40])
            return SyncResult("skipped", "not_post", post, file)

        if post.parse_status is ParseStatus.FAILED:
            # 入库 + 告警，绝不静默丢弃：丢了就等于新格式的帖子永久缺失且无人知晓。
            # 这个指标非 0 就说明频道模板变了，是新写法的早期预警。
            METRICS.incr("sync.parse_failed", channel=channel.key)
            log.warning(
                "sync.parse_failed",
                channel=channel.key,
                message_id=post.message_id,
                text_head=post.raw_text[:80],
            )

        await self._repo.upsert_many([post])
        await self._mark(channel, post.message_id, stored=True)
        METRICS.incr("sync.upserted", channel=channel.key, kind=kind)
        log.info(
            "sync.upserted",
            channel=channel.key,
            kind=kind,
            message_id=post.message_id,
            title=post.title_cn[:40],
            status=str(post.parse_status),
        )
        return SyncResult("upserted", "ok", post, file)

    async def _mark(self, channel: Channel, message_id: int, *, stored: bool) -> None:
        """更新水位。失败只警告 —— 水位是诊断数据，不能因为它丢一条帖子。"""
        try:
            await self._repo.mark_seen(channel.channel_id, message_id, stored=stored)
        except Exception:
            log.warning("sync.mark_failed", channel=channel.key, message_id=message_id)

    async def gap_report(
        self, live_max_id: int | None = None, *, channel: Channel | None = None
    ) -> dict[str, Any]:
        """诊断：同步还在工作吗？索引落后多少？不给频道就是动漫频道。

        `live_max_id` 是频道当前的真实水位，由调用方提供（Bot API 读不到，
        得靠发一条临时消息再删来探）。不给就只比对库里和同步看到的。
        """
        channel = channel or self._cfg.anime
        state = await self._repo.sync_state(channel.channel_id) or {}
        db_max = await self._repo.max_message_id(channel.channel_id)
        seen = int(state.get("last_seen_message_id", 0))

        report: dict[str, Any] = {
            "db_max_message_id": db_max,
            "last_seen_message_id": seen,
            "last_seen_at": state.get("last_seen_at", ""),
            "last_stored_at": state.get("last_stored_at", ""),
            "seen_count": int(state.get("seen_count", 0)),
            "stored_count": int(state.get("stored_count", 0)),
        }
        if live_max_id is not None:
            report["live_max_message_id"] = live_max_id
            report["gap"] = max(0, live_max_id - max(db_max, seen))
        return report
