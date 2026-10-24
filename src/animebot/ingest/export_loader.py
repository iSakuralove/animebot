"""从 Telegram Desktop 导出的 result.json 做全量回填。

Bot API 读不到频道历史消息（没有 getChatHistory / searchMessages，那是 MTProto 的能力），
所以历史帖子只能靠导出文件灌进来一次，之后由 channel_post / edited_channel_post 增量维护。

内置一道 channel_id 校验：讨论群导出里的帖子全是频道帖的转发副本，链接是失效的，
误灌进来会污染整个索引 —— 这个错真实发生过，所以默认拒绝而不是警告。
导出属于哪个频道由文件自己的 id 决定，必须是配置里的某一个（动漫 / 漫画）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import Channel
from ..domain.files import ChannelFile
from ..domain.post import ParseStatus, Post
from ..parsing.files import parse_file
from ..parsing.post_parser import parse_message
from ..storage.repo import PostRepo


class ExportMismatch(RuntimeError):
    """导出文件的频道和配置不符。"""


@dataclass(slots=True)
class IngestStats:
    total: int = 0        # 导出文件里的消息条数
    ok: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0      # 公告、闲聊、service，不是帖子
    upserted: int = 0     # 实际写库的行数
    files: int = 0        # 频道里直接发的文件（文档消息）

    def bump(self, status: ParseStatus) -> None:
        match status:
            case ParseStatus.OK:
                self.ok += 1
            case ParseStatus.PARTIAL:
                self.partial += 1
            case ParseStatus.FAILED:
                self.failed += 1
            case ParseStatus.SKIPPED:
                self.skipped += 1

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def load_export(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        path = path / "result.json"
    if not path.exists():
        raise FileNotFoundError(f"导出文件不存在: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_channel(doc: dict[str, Any], channels: Iterable[Channel]) -> Channel:
    """导出文件属于哪个已配置的频道。都不是就拒绝 —— 理由同讨论群：灌错了是整库污染。"""
    channel_id = int(doc["id"])
    known = list(channels)
    for c in known:
        if c.channel_id == channel_id:
            return c
    listed = "、".join(f"{c.key}={c.channel_id}" for c in known)
    raise ExportMismatch(
        f"导出文件是 {doc.get('name')!r} (type={doc.get('type', '')}, id={channel_id})，"
        f"不是已配置的频道（{listed}）。"
        "如果这是讨论群的导出，里面的帖子都是频道帖的转发副本、链接已失效，不要灌进来。"
    )


def parse_export(
    doc: dict[str, Any],
    *,
    expected_channel_id: int | None = None,
    keep_skipped: bool = False,
) -> tuple[list[Post], IngestStats]:
    channel_id = int(doc["id"])
    chat_type = doc.get("type", "")

    if expected_channel_id is not None and channel_id != expected_channel_id:
        raise ExportMismatch(
            f"导出文件是 {doc.get('name')!r} (type={chat_type}, id={channel_id})，"
            f"但配置的频道 id 是 {expected_channel_id}。"
            "如果这是讨论群的导出，里面的帖子都是频道帖的转发副本、链接已失效，不要灌进来。"
        )
    if "channel" not in chat_type:
        raise ExportMismatch(
            f"导出文件的 type 是 {chat_type!r}，不是频道。"
            "讨论群 (supergroup) 的数据不能当索引源。"
        )

    stats = IngestStats()
    posts: list[Post] = []
    for msg in doc.get("messages", []):
        stats.total += 1
        post = parse_message(msg, channel_id)
        if post is None:
            stats.skipped += 1
            continue
        stats.bump(post.parse_status)
        if post.parse_status is ParseStatus.SKIPPED and not keep_skipped:
            continue
        posts.append(post)
    return posts, stats


def parse_export_files(doc: dict[str, Any]) -> list[ChannelFile]:
    """导出里的文件消息。频道校验由 parse_export 负责，回填总是两个一起调。"""
    channel_id = int(doc["id"])
    return [f for msg in doc.get("messages", []) if (f := parse_file(msg, channel_id))]


async def ingest_doc(
    doc: dict[str, Any],
    repo: PostRepo,
    *,
    source: str,
    expected_channel_id: int | None = None,
    batch_size: int = 500,
    trace_id: str = "",
) -> IngestStats:
    posts, stats = parse_export(doc, expected_channel_id=expected_channel_id)
    files = parse_export_files(doc)

    run_id = await repo.start_run(source=source, trace_id=trace_id)
    try:
        for i in range(0, len(posts), batch_size):
            stats.upserted += await repo.upsert_many(posts[i : i + batch_size])
        stats.files = await repo.upsert_files(files)
    finally:
        await repo.finish_run(
            run_id, stats.as_dict(), note=f"{doc.get('name', '')} · 文件 {stats.files}"
        )
    return stats


async def ingest_export(
    path: str | Path,
    repo: PostRepo,
    *,
    expected_channel_id: int | None = None,
    batch_size: int = 500,
    trace_id: str = "",
) -> IngestStats:
    return await ingest_doc(
        load_export(path),
        repo,
        source=str(path),
        expected_channel_id=expected_channel_id,
        batch_size=batch_size,
        trace_id=trace_id,
    )
