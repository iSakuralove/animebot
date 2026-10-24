"""频道里直接上传的文件（文档消息）。

漫画频道早期的资源就是整卷压缩包，紧跟在资料帖后面发进频道：
#99「进击的巨人」后面是 55 个 zip，#218「范马刃牙」后面是 38 个 rar。
文件消息没有正文，解析器不会把它当帖子 —— 但它属于前面那个帖子。

归属关系不入库：读的时候按「message_id 不大于文件的、最近的那个帖子」现算
（`PostRepo.file_stats`）。两条写入路径都只记事实，归属规则只有一份。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChannelFile:
    channel_id: int
    message_id: int
    file_name: str
    file_size: int          # 字节
    mime_type: str = ""

    @property
    def key(self) -> tuple[int, int]:
        return self.channel_id, self.message_id


@dataclass(frozen=True, slots=True)
class PostFiles:
    """一个帖子名下的文件汇总。结果列表里那句「📦 55个文件 10.8GB」就靠它。"""

    count: int
    total_size: int
    first_message_id: int   # 跳转用：点了直接到第一个文件
