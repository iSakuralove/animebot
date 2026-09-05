"""集中配置。所有环境相关的东西只在这里出现一次。

环境变量前缀 ANIMEBOT_，也可以写进项目根的 .env。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

LinkMode = Literal["public", "internal"]

# 默认启用的功能模块。加新模块 = 往这里加一个名字 + 建一个包，不动别的文件。
DEFAULT_FEATURES = ("system", "search")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="ANIMEBOT_",
        extra="ignore",
    )

    # token 用裸名 AnimeCoffeebot（不带 ANIMEBOT_ 前缀），跟现有 .env 一致
    bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("AnimeCoffeebot", "ANIMEBOT_BOT_TOKEN"),
    )

    # ---- 频道 ----
    # 导出 JSON 里的 id 是不带 -100 前缀的形式，这里保持一致。
    channel_id: int = 1702674582
    channel_username: str = "YXHMd"
    # 总开关：public -> t.me/YXHMd/<id>（任何人可点）
    #        internal -> t.me/c/1702674582/<id>（仅频道成员可见）
    link_mode: LinkMode = "public"

    # 全频道公用的番剧汇总表格，不入库到单条帖子，只在这里出现
    summary_sheet_url: str = (
        "https://docs.google.com/spreadsheets/d/"
        "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX/edit?usp=sharing"
    )

    # ---- 功能模块 ----
    features: tuple[str, ...] = DEFAULT_FEATURES
    admin_ids: tuple[int, ...] = ()

    # ---- 存储 ----
    db_path: Path = ROOT / "data" / "animebot.db"

    # ---- 检索 ----
    search_page_size: int = Field(default=8, ge=1, le=20)
    search_max_candidates: int = Field(default=200, ge=20, le=2000)
    fuzzy_min_score: int = Field(default=55, ge=0, le=100)

    # ---- 外部 API（/bgm、/agent 之类都走这套）----
    http_timeout: float = Field(default=10.0, gt=0)
    http_retries: int = Field(default=2, ge=0, le=5)
    http_cache_ttl: int = Field(default=900, ge=0)

    # ---- 可观测性 ----
    log_level: str = "INFO"
    log_json: bool = False          # 控制台是否也输出 JSON；文件永远是 JSON
    log_dir: Path = ROOT / "logs"
    telemetry_enabled: bool = True
    slow_command_ms: float = 1500.0  # 超过就打 warning，便于抓慢指令

    @field_validator("channel_id")
    @classmethod
    def _strip_prefix(cls, v: int) -> int:
        """容忍误填 -1001702674582 这种 Bot API 形式。"""
        s = str(v)
        if s.startswith("-100"):
            return int(s[4:])
        return abs(v)

    @field_validator("features", "admin_ids", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """支持 ANIMEBOT_FEATURES=system,search,bgm 这种写法。"""
        if isinstance(v, str):
            return tuple(x.strip() for x in v.replace(";", ",").split(",") if x.strip())
        return v

    @property
    def link_username(self) -> str | None:
        """喂给 Post.permalink()；internal 模式返回 None 走 /c/ 链接。"""
        if self.link_mode == "public" and self.channel_username:
            return self.channel_username.lstrip("@")
        return None

    @property
    def bot_api_chat_id(self) -> int:
        """Bot API 的 chat_id 需要 -100 前缀，copyMessage/forwardMessage 用这个。"""
        return int(f"-100{self.channel_id}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
