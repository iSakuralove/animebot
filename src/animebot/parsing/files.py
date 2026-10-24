"""文件消息 -> ChannelFile。

和 `parse_message` 并列：一条消息可以是帖子、是文件、或者两者都是（带说明文字
的压缩包）。两条数据入口（导出 JSON / 增量 update）都先变成导出 JSON 的形状再
进这里，和帖子解析同一个理由 —— 解析逻辑只有一份，就不会漂移。
"""

from __future__ import annotations

from typing import Any

from ..domain.files import ChannelFile


def parse_file(msg: dict[str, Any], channel_id: int) -> ChannelFile | None:
    """只认文档。

    导出里贴纸、视频、语音、GIF 也有 `file` 键，但它们都带 `media_type`；
    文档没有。漫画频道 272 个 `file` 里 271 个是 zip/rar/7z，剩下那个是贴纸。
    """
    if msg.get("type") != "message" or "file" not in msg or msg.get("media_type"):
        return None
    return ChannelFile(
        channel_id=channel_id,
        message_id=int(msg["id"]),
        file_name=str(msg.get("file_name") or ""),
        file_size=int(msg.get("file_size") or 0),
        mime_type=str(msg.get("mime_type") or ""),
    )
