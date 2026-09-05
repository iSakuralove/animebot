# 加一个新模块

以 `/bgm`（查 Bangumi）为例。真实参照物是 `src/animebot/features/system/` 和
`features/search/`，两个都是按这套写的，没有特权。

**不需要改 `app.py`、`cli.py` 或任何已有文件。**

## 1. 建目录

```
src/animebot/features/bgm/
    __init__.py     模块定义，导出 FEATURE
    handlers.py     @command 指令
    client.py       Bangumi API 客户端（继承 HttpClient）
```

`.env` 里加上模块名：

```
ANIMEBOT_FEATURES=system,search,bgm
```

## 2. `__init__.py`

```python
"""bgm 模块：查 Bangumi 的番剧信息。"""

from __future__ import annotations

from aiogram import Router

from ...core.feature import BaseFeature
from ...core.http import HttpClient
from . import handlers


class BgmFeature(BaseFeature):
    name = "bgm"
    requires = ("registry",)          # 启动期校验依赖，缺了立刻炸

    def __init__(self, settings, container) -> None:
        super().__init__(settings, container)
        self._client = HttpClient(
            "Bangumi",
            base_url="https://api.bgm.tv",
            timeout=settings.http_timeout,
            retries=settings.http_retries,
            cache_ttl=settings.http_cache_ttl,
            headers={"User-Agent": "animebot/0.1"},
        )

    async def setup(self) -> None:
        await self._client.open()
        # 放进容器，handler 声明同名参数就能拿到
        self.container.put("bgm_client", self._client, closer=self._client.close)

    async def teardown(self) -> None:
        await self._client.close()

    def router(self) -> Router:
        from ...bot.wiring import build_feature_router
        return build_feature_router(self, handlers, self.container.get("registry"))

    def health(self) -> dict[str, object]:
        return {"status": "ok", "cache_entries": self._client.cache_size()}


FEATURE = BgmFeature
```

## 3. `handlers.py`

```python
from __future__ import annotations

from aiogram.types import Message

from ..._util import command_arg
from ...core.errors import NotFound, UsageError
from ...core.http import HttpClient
from ...core.registry import command
from ...observability.context import bind
from ...observability.metrics import METRICS


@command(
    "bgm",
    desc="查 Bangumi 番剧信息",
    usage="/bgm 无职英雄",
    rate=(3, 10),                     # 10 秒内最多 3 次，中间件自动执行
    long_help="从 Bangumi 拉取评分、放送日期、制作人员。",
)
async def cmd_bgm(message: Message, bgm_client: HttpClient) -> None:
    """参数名 bgm_client 对应容器里的键，自动注入。声明什么就拿到什么。"""
    keyword = command_arg(message)
    if not keyword:
        raise UsageError("要查什么？", "/bgm 无职英雄")

    bind(keyword=keyword)             # 这个字段会出现在本次请求的所有日志里

    data = await bgm_client.get_json(
        f"/search/subject/{keyword}", params={"type": 2, "max_results": 5}
    )
    items = data.get("list") or []
    METRICS.incr("bgm.search", found=bool(items))
    if not items:
        raise NotFound(f"Bangumi 上的 {keyword}")

    await message.reply(render(items))
```

## 4. 自动获得什么

不写任何额外代码就有：

| 能力 | 来源 |
|---|---|
| `trace_id` 贯穿全链路 | `TraceMiddleware` + contextvars，每条日志自动带 |
| 耗时埋点 + 慢指令告警 | `ObservabilityMiddleware`，`handler.latency{command=bgm}` |
| 调用计数分 outcome | `handler.calls{command=bgm,outcome=ok\|user_error\|error}` |
| 错误边界 | `UserError` 原文给用户；真异常给编号 + 完整栈进日志 |
| 限流 | `@command(rate=...)` 声明即生效，按 `(user_id, 指令)` 隔离 |
| 权限 / 场景 | `admin_only=True`、`group_allowed=False` |
| `/help` 收录 | 从 `CommandRegistry` 生成，不用手写帮助 |
| Telegram 指令菜单 | 启动时自动 `setMyCommands` |
| HTTP 超时/退避/缓存/埋点 | 继承 `HttpClient` |

唯一的约定：**抛 `UserError` 子类表示「用户的问题」，其它异常表示「我们的 bug」。**
这条边界决定了用户看到的是提示还是错误编号。

## 5. 验证

```bash
uv run animebot commands
```

不连 Telegram，确认指令登记成功、别名没冲突、权限标记对。

```bash
uv run pytest
```

```bash
uv run animebot run
```

## 埋点该埋在哪

中间件已经覆盖了「每个指令的调用数、耗时、成败」。模块自己只需要埋
**业务语义**的点 —— 中间件不可能知道的东西：

```python
METRICS.incr("bgm.search", found=bool(items))     # 查到了没有
METRICS.observe("bgm.results", len(items))        # 返回了几条
bind(subject_id=items[0]["id"])                   # 这次请求的业务主键
```

不要重复埋耗时和调用数，中间件已经做了。
