"""帖子领域模型。

主键是 (channel_id, message_id) —— Telegram 自己的天然主键，不另造 id。
raw_text 和 parse_status 是强制字段：1486 个帖子跨四年半发出，格式漂移是常态，
解析失败必须留下现场而不是丢数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ParseStatus(StrEnum):
    OK = "ok"            # 拿到标题 + 至少一个可用信号
    PARTIAL = "partial"  # 拿到标题，但字段少得可疑
    FAILED = "failed"    # 连标题都没有
    SKIPPED = "skipped"  # 不是帖子（公告 / service / 纯闲聊）


@dataclass(slots=True)
class Post:
    channel_id: int
    message_id: int
    #: 一律 aware UTC。两条数据入口（导出 JSON / aiogram update）都在各自的
    #: 解析层归一化到 UTC —— 混用 naive 和 aware 会让 ORDER BY posted_at
    #: 在两批数据间错开 8 小时（导出的 date 是 +08:00 本地时间）。
    posted_at: datetime
    edited_at: datetime | None = None

    title_cn: str = ""
    title_en: str = ""
    aliases: list[str] = field(default_factory=list)

    episodes: str = ""
    air_date: str = ""
    air_weekday: str = ""
    duration: str = ""

    score: float | None = None
    score_text: str = ""          # "不过不失" / "推荐" / "力荐"

    summary: str = ""
    staff: dict[str, str] = field(default_factory=dict)

    tags: list[str] = field(default_factory=list)        # #轻改 #奇幻
    index_tags: list[str] = field(default_factory=list)  # #W #WZ 拼音首字母

    passwords: list[str] = field(default_factory=list)   # 解压密码，可能有备用
    links: dict[str, str] = field(default_factory=dict)  # baidu/onedrive/gdrive/...

    has_photo: bool = False
    raw_text: str = ""
    extra: dict[str, str] = field(default_factory=dict)  # 未知字段一律留着

    parse_status: ParseStatus = ParseStatus.OK
    parse_notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return self.channel_id, self.message_id

    @property
    def search_title(self) -> str:
        """检索用的标题串：中文名 + 英文名 + 别名拼一起，一次 LIKE 全覆盖。"""
        parts = [self.title_cn, self.title_en, *self.aliases]
        return " ".join(p for p in parts if p)

    def permalink(self, username: str | None = None) -> str:
        """公开频道用 username 深链；否则退回 /c/ 内部链接（仅成员可见）。"""
        if username:
            return f"https://t.me/{username.lstrip('@')}/{self.message_id}"
        return f"https://t.me/c/{self.channel_id}/{self.message_id}"
