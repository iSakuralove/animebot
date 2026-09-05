"""从 Telegram Desktop 导出的 result.json 做全量回填。

Bot API 读不到频道历史消息（没有 getChatHistory / searchMessages，那是 MTProto 的能力），
所以历史帖子只能靠导出文件灌进来一次，之后由 channel_post / edited_channel_post 增量维护。

内置一道 channel_id 校验：讨论群导出里的帖子全是频道帖的转发副本，链接是失效的，
误灌进来会污染整个索引 —— 这个错真实发生过，所以默认拒绝而不是警告。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..domain.post import ParseStatus, Post
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


async def ingest_export(
    path: str | Path,
    repo: PostRepo,
    *,
    expected_channel_id: int | None = None,
    batch_size: int = 500,
    trace_id: str = "",
) -> IngestStats:
    doc = load_export(path)
    posts, stats = parse_export(doc, expected_channel_id=expected_channel_id)

    run_id = await repo.start_run(source=str(path), trace_id=trace_id)
    try:
        for i in range(0, len(posts), batch_size):
            stats.upserted += await repo.upsert_many(posts[i : i + batch_size])
    finally:
        await repo.finish_run(run_id, stats.as_dict(), note=doc.get("name", ""))
    return stats
