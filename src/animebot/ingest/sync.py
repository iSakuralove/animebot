"""频道帖增量同步。

回填灌进来的是 2026-09-05 的快照。频道还在发新帖，而且**每一条帖子都会被
编辑** —— 真实数据里 1486/1486 有 edited 字段，作者会在链接失效后回去改。
只索引新帖不管编辑，等于索引里永远存着第一版内容。

所以 channel_post 和 edited_channel_post 走完全相同的路径：upsert 幂等，
不需要区分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.types import Message

from ..config import Settings
from ..domain.post import ParseStatus, Post
from ..observability.logging import get_logger
from ..observability.metrics import METRICS
from ..parsing.post_parser import parse_message
from ..storage.repo import PostRepo
from .update_adapter import message_to_export_shape

log = get_logger("animebot.sync")


@dataclass(frozen=True, slots=True)
class SyncResult:
    """同步一条消息的结果。skipped 的原因要能区分，否则排查时只知道"没入库"。"""

    action: str          # upserted | skipped
    reason: str          # ok | wrong_chat | not_post | empty | parse_failed
    post: Post | None = None

    @property
    def stored(self) -> bool:
        return self.action == "upserted"


class ChannelSync:
    """频道帖 -> 索引库。与回填共用 parse_message 和 upsert_many。"""

    def __init__(self, repo: PostRepo, settings: Settings) -> None:
        self._repo = repo
        self._cfg = settings

    def accepts(self, msg: Message) -> bool:
        """只收自己频道的帖子。

        讨论群里的同名帖子是频道帖的自动转发副本，链接已失效（帖子正文自己就
        写着「讨论中的链接是失效的」）。灌进来会让搜索结果指向死链。这跟回填
        那道 channel_id 校验是同一个理由。
        """
        return msg.chat.id == self._cfg.bot_api_chat_id

    async def handle(self, msg: Message, *, kind: str = "new") -> SyncResult:
        METRICS.incr("sync.received", kind=kind)

        if not self.accepts(msg):
            METRICS.incr("sync.skipped", reason="wrong_chat")
            log.debug("sync.wrong_chat", chat_id=msg.chat.id, message_id=msg.message_id)
            return SyncResult("skipped", "wrong_chat")

        with METRICS.timer("sync.latency", kind=kind):
            post = parse_message(
                message_to_export_shape(msg), self._cfg.channel_id
            )

        if post is None:
            # 媒体组的第 2~n 张图没有 caption，正文为空 —— 这是正常情况，不是错误。
            # 仍要记水位：它证明 update 还在到达。
            await self._mark(msg.message_id, stored=False)
            METRICS.incr("sync.skipped", reason="empty")
            return SyncResult("skipped", "empty")

        if post.parse_status is ParseStatus.SKIPPED:
            await self._mark(post.message_id, stored=False)
            METRICS.incr("sync.skipped", reason="not_post")
            log.debug("sync.not_post", message_id=post.message_id)
            return SyncResult("skipped", "not_post", post)

        if post.parse_status is ParseStatus.FAILED:
            # 入库 + 告警，绝不静默丢弃：丢了就等于新格式的帖子永久缺失且无人知晓。
            # 这个指标非 0 就说明频道模板变了，是新写法的早期预警。
            METRICS.incr("sync.parse_failed")
            log.warning(
                "sync.parse_failed",
                message_id=post.message_id,
                text_head=post.raw_text[:80],
            )

        await self._repo.upsert_many([post])
        await self._mark(post.message_id, stored=True)
        METRICS.incr("sync.upserted", kind=kind)
        log.info(
            "sync.upserted",
            kind=kind,
            message_id=post.message_id,
            title=post.title_cn[:40],
            status=str(post.parse_status),
        )
        return SyncResult("upserted", "ok", post)

    async def _mark(self, message_id: int, *, stored: bool) -> None:
        """更新水位。失败只警告 —— 水位是诊断数据，不能因为它丢一条帖子。"""
        try:
            await self._repo.mark_seen(self._cfg.channel_id, message_id, stored=stored)
        except Exception:
            log.warning("sync.mark_failed", message_id=message_id)

    async def gap_report(self, live_max_id: int | None = None) -> dict[str, Any]:
        """诊断：同步还在工作吗？索引落后多少？

        `live_max_id` 是频道当前的真实水位，由调用方提供（Bot API 读不到，
        得靠发一条临时消息再删来探）。不给就只比对库里和同步看到的。
        """
        state = await self._repo.sync_state(self._cfg.channel_id) or {}
        db_max = await self._repo.max_message_id(self._cfg.channel_id)
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
