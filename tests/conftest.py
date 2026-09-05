"""pytest 公共夹具。

黄金数据集在 tests/test_parser_golden.py 里用到；这里提供内存库、
造好的 Post、以及一个能把 handler 跑起来的最小 App。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from animebot.config import Settings
from animebot.domain.post import ParseStatus, Post
from animebot.search.service import SearchService
from animebot.storage.repo import PostRepo

EXPORT_PATH = Path(
    r"C:\Users\Administrator\Downloads\Telegram Desktop\频道json数据\result.json"
)
CHANNEL_ID = 1702674582


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="test:token",
        channel_id=CHANNEL_ID,
        channel_username="YXHMd",
        link_mode="public",
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        features=("system", "search"),
        admin_ids=(42,),
    )


@pytest.fixture
async def repo(settings: Settings) -> AsyncIterator[PostRepo]:
    async with PostRepo(settings.db_path) as r:
        await r.init_schema()
        yield r


@pytest.fixture
def search(repo: PostRepo, settings: Settings) -> SearchService:
    return SearchService(repo, settings)


@pytest.fixture
def search_service_factory(repo: PostRepo, settings: Settings) -> SearchService:
    """与 `search` 同一个对象，另起名字给 sync 测试用 —— 那里 `sync` 已经
    是夹具名，再叫 `search` 会让「同步后能否搜到」这个断言的意图变模糊。"""
    return SearchService(repo, settings)


def make_post(
    message_id: int = 1,
    title_cn: str = "测试番剧",
    **kw: object,
) -> Post:
    """造一个最小可用的 Post。只写测试关心的字段。"""
    defaults: dict[str, object] = {
        "channel_id": CHANNEL_ID,
        "message_id": message_id,
        "posted_at": dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=dt.UTC),
        "title_cn": title_cn,
        "parse_status": ParseStatus.OK,
    }
    defaults.update(kw)
    return Post(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def sample_posts() -> list[Post]:
    return [
        make_post(
            1,
            "无职英雄 ～技能什么的毫无用处～",
            title_en="Mushoku no Eiyuu",
            episodes="12",
            air_date="2025年10月1日",
            score=4.9,
            score_text="不过不失",
            tags=["轻改", "奇幻", "异世界"],
            index_tags=["W", "WZ"],
            links={"baidu": "https://pan.baidu.com/s/x?pwd=0000"},
            passwords=["blackcatunderthemoon"],
            summary="一个没有技能的无职之人的故事",
        ),
        make_post(2, "英雄教室", episodes="12", score=5.3, tags=["奇幻", "校园"]),
        make_post(3, "咒术回战", episodes="24", score=7.8, tags=["漫改", "热血"]),
        make_post(4, "白色相簿2", episodes="13", score=7.3, tags=["恋爱", "校园"]),
        make_post(5, "白色相簿", episodes="13", score=7.3, tags=["恋爱", "偶像"]),
    ]
