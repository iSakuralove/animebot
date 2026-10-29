"""集中配置。所有环境相关的东西只在这里出现一次。

环境变量前缀 ANIMEBOT_，也可以写进项目根的 .env。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

LinkMode = Literal["public", "internal"]

# 默认启用的功能模块。加新模块 = 往这里加一个名字 + 建一个包，不动别的文件。
DEFAULT_FEATURES = ("system", "search", "manga", "ai")


@dataclass(frozen=True, slots=True)
class Channel:
    """一个被索引的频道。

    所有频道共用一个库、一个解析器 —— 它们是同一个 up 主、同一套发帖模板。
    channel_id 是查询维度：检索、标签云、同步水位都按它隔开。
    """

    key: str            # anime / manga：日志、埋点、/health 里用它认频道
    title: str          # 给人看的名字
    channel_id: int     # 导出 JSON 的形式，不带 -100 前缀
    username: str = ""

    @property
    def bot_api_chat_id(self) -> int:
        """Bot API 的 chat_id 需要 -100 前缀。"""
        return int(f"-100{self.channel_id}")

    def link_username(self, mode: LinkMode) -> str | None:
        """喂给 Post.permalink()。internal 模式或没配 username 时返回 None 走 /c/ 链接。"""
        if mode == "public" and self.username:
            return self.username.lstrip("@")
        return None


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
    #        internal -> t.me/c/1702674582/<id>（仅频道成员可见，默认）
    # 默认 internal：资源是频道福利，链接只对已加入频道的人有意义。
    link_mode: LinkMode = "internal"

    # 全频道公用的番剧汇总表格，不入库到单条帖子，只在这里出现
    summary_sheet_url: str = (
        "https://docs.google.com/spreadsheets/d/"
        "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX/edit?usp=sharing"
    )

    # ---- 漫画频道（/c 搜它）----
    # ACG Library 二次元图书馆。导出里没有 username，没配时 public 模式也退回
    # /c/ 内部链接 —— 启动自检会把频道真实的 username 打进日志。
    manga_channel_id: int = 1659887654
    manga_channel_username: str = ""

    # ---- 功能模块 ----
    # NoDecode：这两个是 tuple（复杂类型），pydantic-settings 默认会先对 env 值
    # 做 json.loads，"system,search" 不是合法 JSON 直接炸。NoDecode 关掉那步，
    # 把裸字符串交给下面的 _split_csv 验证器按逗号拆。
    features: Annotated[tuple[str, ...], NoDecode] = DEFAULT_FEATURES
    admin_ids: Annotated[tuple[int, ...], NoDecode] = ()

    # ---- 存储 ----
    db_path: Path = ROOT / "data" / "animebot.db"

    # ---- 检索 ----
    search_page_size: int = Field(default=8, ge=1, le=20)
    # 候选集上限。必须 >= 全库帖子数，否则 SearchPage.total 会是个谎 ——
    # 分页之前这只影响"少显示几条"，分页之后总数是用户可见的数字：
    # #漫改 真实 572 条，cap=200 时界面显示"共 200 条 / 25 页"。
    # 实测最坏情况（#漫改，572 条全排序）34ms，远在 100ms 目标内。
    # 全库到 ~5 万条时这个数要重新评估，届时该换的是倒排索引而不是调大它。
    search_max_candidates: int = Field(default=3000, ge=20, le=50000)
    # 65 是实测分界线：「天下」顶格 60 分（只沾一个字，是噪音），而「咒术回站」
    # →咒术回战 75 分、「无值英雄」→无职英雄 67.5 分（真错字）。55 会把「天下」
    # 凑出 17 条相关但不匹配的垃圾，违反「不指定指令只返回真匹配」的业务铁律。
    fuzzy_min_score: int = Field(default=65, ge=0, le=100)
    # 超长查询词（>64 字节）的 LRU 暂存容量。丢了只会让翻页提示"搜索已过期"。
    query_store_size: int = Field(default=512, ge=16, le=10000)

    # 连 api.telegram.org 的代理。空 = 直连。这台机器直连被墙，需要 http 代理。
    # 例：ANIMEBOT_PROXY=http://127.0.0.1:7898
    proxy: str = ""

    # ---- 外部 API（/bgm、/agent 之类都走这套）----
    http_timeout: float = Field(default=10.0, gt=0)
    http_retries: int = Field(default=2, ge=0, le=5)
    http_cache_ttl: int = Field(default=900, ge=0)

    # ---- AI 对话（/q）----
    # 走 Anthropic Messages 协议（/v1/messages + SSE）。这个网关是 OpenAI 兼容的
    # 再包一层 Claude 协议，所以请求体按 Claude 写、模型名填它自己的。
    ai_base_url: str = "https://happycoding.xyz"
    ai_api_key: SecretStr = SecretStr("")
    # 首选 + 兜底。主模型 5xx/超时会退到下一个 —— 一个网关挂掉不该让 /q 整个不可用。
    ai_model: str = "deepseek-v4.1-flash"
    ai_fallback_model: str = "glm-5.3-flash"
    # 单次回答上限。这家的模型强制 thinking，思考也算 token —— 2000 是
    # 「能写完一篇解释」和「别把余额烧穿」之间的折中。
    ai_max_tokens: int = Field(default=2000, ge=256, le=8000)
    # 工具循环上限。模型不会自己停，超了就在这一轮强制它总结。
    ai_max_tool_rounds: int = Field(default=4, ge=0, le=8)
    # 单次请求（含所有工具轮次）的总超时。流式断了也必须给用户一个交代。
    ai_timeout: float = Field(default=120.0, gt=0)
    # 多轮上下文：闲置多久后清空；保留多少条消息（user+assistant 各算一条）。
    ai_session_ttl: float = Field(default=1800.0, gt=0)
    ai_session_messages: int = Field(default=16, ge=2, le=64)
    # 联网搜索走 Bing（VPS 直连可用，DDG 会返回人机验证页）。
    ai_search_timeout: float = Field(default=15.0, gt=0)
    # Bangumi Access Token（next.bgm.tv/demo/access-token）。带上之后限流更宽，
    # 而且能拿到 NSFW 标记做过滤。不带也能用，只是慢且容易被 429。
    bgm_token: SecretStr = SecretStr("")

    # ---- 可观测性 ----
    log_level: str = "INFO"
    log_json: bool = False          # 控制台是否也输出 JSON；文件永远是 JSON
    log_dir: Path = ROOT / "logs"
    telemetry_enabled: bool = True
    # 超过就打 warning。设 5000 是因为走代理时每次 API 往返 2~3s 是常态，
    # 1500 会把每条正常翻页都误报成慢查询、刷满日志。直连部署后可以调回 1500。
    slow_command_ms: float = 5000.0

    @field_validator("channel_id", "manga_channel_id")
    @classmethod
    def _strip_prefix(cls, v: int) -> int:
        """容忍误填 -1001702674582 这种 Bot API 形式。"""
        s = str(v)
        if s.startswith("-100"):
            return int(s[4:])
        return abs(v)

    @model_validator(mode="after")
    def _distinct_channels(self) -> Self:
        """两个频道配成同一个 id，同步会把帖子记到错的频道名下，检索结果串台。"""
        if self.channel_id == self.manga_channel_id:
            raise ValueError(
                f"ANIMEBOT_CHANNEL_ID 和 ANIMEBOT_MANGA_CHANNEL_ID 都是 {self.channel_id}"
            )
        return self

    @field_validator("features", "admin_ids", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """支持 ANIMEBOT_FEATURES=system,search,bgm 这种写法。

        admin_ids 拆完还是 str 元组，交给 pydantic 把每个转成 int。
        """
        if isinstance(v, str):
            return tuple(x.strip() for x in v.replace(";", ",").split(",") if x.strip())
        return v

    @property
    def anime(self) -> Channel:
        return Channel("anime", "动漫", self.channel_id, self.channel_username)

    @property
    def manga(self) -> Channel:
        return Channel("manga", "漫画", self.manga_channel_id, self.manga_channel_username)

    @property
    def channels(self) -> tuple[Channel, ...]:
        """所有被索引的频道。回填、同步、自检都遍历它，不认具体是哪个。"""
        return (self.anime, self.manga)

    def channel_by_id(self, channel_id: int) -> Channel | None:
        return next((c for c in self.channels if c.channel_id == channel_id), None)

    @property
    def link_username(self) -> str | None:
        """动漫频道的深链 username；internal 模式返回 None 走 /c/ 链接。"""
        return self.anime.link_username(self.link_mode)

    @property
    def bot_api_chat_id(self) -> int:
        """动漫频道的 Bot API chat_id（带 -100 前缀）。"""
        return self.anime.bot_api_chat_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
