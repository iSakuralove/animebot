# core —— 内核

`src/animebot/core/`

## 职责

提供所有模块共享的机制，**不含任何业务知识**，也**不 import aiogram**
—— 内核要能在没有 Telegram 的环境里跑（CLI、测试）。

| 文件 | 职责 |
|---|---|
| `registry.py` | `@command` 装饰器 + `CommandRegistry` |
| `container.py` | 依赖容器 |
| `feature.py` | 模块协议 `BaseFeature` |
| `errors.py` | 错误分类 |
| `http.py` | 外部 API 基类 |

## registry —— 指令注册表

`@command` **只贴元数据，不包裹函数**。理由见
[ADR-0001](../ADR/0001-metadata-only-decorator.md)。

### 契约

- 装饰器往函数上设 `__animebot_command__` 属性，值是 `CommandSpec`
- `CommandSpec` 是 `slots=True` 的 dataclass，**没有 `__dict__`**
  —— 要复制它用 `spec.with_feature(name)`（内部是 `dataclasses.replace`），
  不要手抄字段。踩过这个坑：手抄 11 个字段，加字段就会漏
- `feature` 字段由 `bind_commands()` 在装配时填，装饰器里是空串
- 重名（含别名冲突）在 `registry.add()` 里抛 `ConfigError` —— **启动期就炸**，
  不会变成「运行时随机命中一个」
- `get()` 容忍前导斜杠和大小写：`get("/SEARCH")` 有效

### 谁读这张表

一处登记，四处使用：

```
@command  →  CommandRegistry  →  ┬→ Router 绑定（Command 过滤器）
                                 ├→ /help 生成
                                 ├→ setMyCommands 同步
                                 └→ 中间件读 rate / admin_only / group_allowed
```

所以不存在「加了指令忘记更新帮助」。

## container —— 依赖容器

不是 DI 框架，就是一个带生命周期的字典。

### 契约

- `put(name, obj, closer=None)` —— 重复 put 同名键抛 `ConfigError`
- `get(name)` —— 缺失时的错误消息里**列出已注册的键**，便于排查拼写
- `require(*names)` —— 启动期批量校验，模块的 `requires` 走这个
- `aclose()` —— **逆序**关闭；单个 closer 失败不影响其它，
  最后汇总抛 `RuntimeError`

最后一条是刻意的：一个坏连接不能卡死整个关停。有测试
`test_one_bad_closer_does_not_block_others` 盯着。

### 注入怎么生效

```
container.put("repo", repo)
        ↓
dp["repo"] = container.get("repo")       # app.py 里手动搭桥
        ↓
aiogram 把 workflow_data 当 kwargs 传给 handler
        ↓
async def cmd_search(message, repo, settings)    # 参数名匹配，自动拿到
```

中间那一步是手动的 —— 容器和 aiogram 的 workflow_data 是两套东西，
`_build_dispatcher()` 里把需要的键搬过去。这比自动同步全部键更可控。

## feature —— 模块协议

```python
class BaseFeature:
    name: str
    requires: tuple[str, ...] = ()      # 启动期校验的依赖名

    async def setup(self) -> None       # 建连接、预热。抛异常 = 启动失败
    async def teardown(self) -> None    # 必须容忍 setup 没跑完就被调用
    def router(self) -> Router          # 子类必须实现
    def health(self) -> dict            # 给 /health 用
```

### 坑

**`teardown()` 必须幂等且容忍未初始化。** 如果 `setup()` 中途抛异常，
`build_app()` 的 `except BaseException` 会调用 `app.aclose()`，
而它会对**所有已加载的** feature 调 `teardown()` —— 包括 setup 失败的那个。

**`health()` 目前没被 `/health` 调用。** 这是已知的债，记在
[operations.md](../operations.md) 里 —— 心跳推送要用到它，届时一起做。

## errors —— 错误分类

只有两类，边界必须清楚：

```
BotError
├── UserError ──────────────── 用户能修正。原文给用户，日志 info
│   ├── UsageError            参数错，附用法
│   ├── PermissionDenied
│   ├── RateLimited           带 retry_after
│   └── NotFound
├── ExternalServiceError ───── 外部 API 挂了
└── ConfigError ───────────── 配置错，启动期炸

其它 Exception ─────────────── 我们的 bug。用户拿 trace_id，完整栈进日志
```

### `ExternalServiceError` 的双消息设计

```python
e = ExternalServiceError("Bangumi", "HTTP 503 upstream timeout")
str(e)            # "Bangumi 调用失败: HTTP 503 upstream timeout"   → 进日志
e.user_message    # "Bangumi 暂时用不了，稍后再试"                    → 给用户
```

技术细节进日志，用户只看到人话。有测试
`test_error_detail_hidden_from_user` 断言 `503` 不出现在 `user_message` 里。

### 唯一的约定

**抛 `UserError` 子类 = 用户的问题；其它异常 = 我们的 bug。**

这条边界不划清楚，最后一定是「要么把栈追踪喷给用户，要么把真 bug 咽掉」。
新模块只需要遵守这一条。

## http —— 外部 API 基类

`/bgm` 查 Bangumi、`/agent` 调 LLM，凡是出网的都从这里继承。

### 为什么要有这层

每个外部服务都需要超时、重试、缓存、埋点、错误翻译。每次重写都会漏一样。

### 契约

| 能力 | 行为 |
|---|---|
| 超时 | `aiohttp.ClientTimeout(total=timeout)` |
| 重试 | 只重试 `408/429/500/502/503/504` 和网络错误。**4xx 不重试** |
| 退避 | 指数 + 抖动；有 `Retry-After` 头就用它（上限 30s） |
| 缓存 | 进程内 TTL，key 是 `blake2b(method|url|sorted(params))` |
| 埋点 | `http.calls{service,status}`、`http.latency{service}`、`http.cache_hit{service}` |
| 错误 | 统一翻译成 `ExternalServiceError` |

**4xx 不重试**是刻意的：404 意味着上游明确说没有，重试三次是浪费三倍时间。
有测试 `test_4xx_not_retried` 断言只发了 1 次请求。

**退避带抖动**是为了避免多个请求同时醒来又一起打过去 —— 没有抖动的指数退避
会把并发请求同步成脉冲。

### 坑

**`get_json` 用 `content_type=None`。** 有些上游返回 `text/plain` 但内容是 JSON。
有测试覆盖。

**缓存是进程内的，重启即失效。** 番剧元数据一天内不会变，TTL 默认 900s。
真需要跨重启缓存时该加一张 SQLite 表，但那时缓存的语义也变了（要考虑失效策略），
不是简单换个后端。

**`close()` 后再调 `get_json()` 会自动重开 session。** 有测试
`test_reopen_after_close`。这是为了让容器的 closer 和实际使用顺序解耦。

## 测试

- [test_core.py](../../tests/test_core.py) —— 注册表、容器、错误、上下文、埋点
- [test_http.py](../../tests/test_http.py) —— 用本地 aiohttp 测试服务器，不出网
