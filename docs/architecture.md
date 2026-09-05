# 架构总览

## 一句话

四层单向依赖 + 中间件链 + 按名字加载的模块。加功能只往 `features/` 加包，不改任何已有文件。

## 分层与依赖方向

依赖只能向下。任何向上的箭头都是设计错误。

```
        features/          业务模块（system, search, bgm, agent...）
            │              指令用 @command 声明，只写业务逻辑
            ▼
    ┌───────┴────────┬──────────────┐
    │                │              │
   bot/           search/        ingest/     应用层
   装配、中间件      检索、展示      回填、同步
    │                │              │
    └───────┬────────┴──────────────┘
            ▼
      ┌─────┴──────┬──────────┐
      │            │          │
   storage/    parsing/    domain/          领域层
   SQLite 仓储  文本解析    模型 + 别名表
      │            │          │
      └─────┬──────┴──────────┘
            ▼
      ┌─────┴───────────┐
      │                 │
    core/         observability/              内核
  注册表、容器        trace、日志、埋点
  错误、HTTP
      │                 │
      └─────┬───────────┘
            ▼
        config.py                            配置
```

三条硬规矩：

1. **`core/` 不 import aiogram。** 内核要能在没有 Telegram 的环境里跑（CLI、测试）。
   [container.py](../src/animebot/core/container.py) 和
   [registry.py](../src/animebot/core/registry.py) 都只依赖标准库和自己。
2. **`domain/` 不 import 任何上层。** 它是数据形状，谁都可以依赖它，它谁都不依赖。
3. **`features/` 之间不互相 import。** 要共享就下沉到 `core/` 或建一个 service 放进容器。

## 一次请求的完整旅程

用户发 `/search 无职英雄`：

```
Telegram
  │
  ▼ getUpdates 拉到 Update
Dispatcher
  │
  ├─ TraceMiddleware ─────────── 开 RequestContext，塞 contextvars
  │     生成 8 位 trace_id；解析出 command=search / payload=无职英雄
  │     从 registry 查到 feature=search
  │     之后所有日志自动带这些字段，业务代码不用传
  │
  ├─ ErrorBoundary ──────────────  唯一的兜底出口
  │     UserError    → 原文给用户，日志 info
  │     BotError     → user_message + trace_id
  │     其它 Exception → 「内部错误，编号 a1b2c3d4」+ 完整栈进日志
  │
  ├─ AccessMiddleware ─────────── admin_only / group_allowed
  │     不通过就抛 PermissionDenied（是 UserError，被上面兜住）
  │
  ├─ RateLimitMiddleware ──────── 按 (user_id, 指令) 滑动窗
  │     参数来自 @command(rate=(6, 20))
  │     超了抛 RateLimited（也是 UserError）
  │
  ├─ ObservabilityMiddleware ──── 计时 + 埋点
  │     handler.calls{command=search,feature=search,outcome=ok}
  │     handler.latency{...}；超过 slow_command_ms 打 warning
  │
  ▼
Router (feature.search)
  │  Command("search","s","find") 过滤器命中
  ▼
_adapt() 包装层 ─────────────────  按 handler 签名过滤 kwargs
  │  handler 只声明它要的参数，多余的 data 键不传进去
  ▼
cmd_search(message, search, settings, trace)
  │  参数名 → 容器里的键，自动注入
  ▼
SearchService.search_page()
  │  Query.parse → 拆词和标签
  │  repo.like_titles() 逐词取候选（并集）
  │  rank_terms() 打分，sort_key 全序排序
  │  没命中 → _fuzzy() rapidfuzz 兜底
  │  切出当前页 → SearchPage(hits, total, page, pages)
  ▼
presenter.render_page() + page_keyboard()
  │  高亮：先在原文定位，再逐段转义
  │  callback_data: p:<message_id> / s:<page>:<query> / t:<page>:<token>
  ▼
message.reply()
```

翻页时走同一条路，只是把 `message.reply()` 换成 `callback.message.edit_text()`。
分页状态全在 `callback_data` 里，服务端不存结果集
（[ADR-0010](ADR/0010-pagination-state-in-callback-data.md)）。

### 中间件顺序为什么是这个

顺序不是随便排的，代码里也注释了（[app.py](../src/animebot/bot/app.py)）：

- **Trace 最外** —— 它下游的所有日志都要 trace_id。放第二层就有一段日志没有上下文。
- **ErrorBoundary 次之** —— 它必须能兜住 Access 和 RateLimit 抛的 `UserError`，
  所以那两个必须在它**内侧**。反过来的话，限流提示会变成未捕获异常。
- **Observability 在它们内侧** —— 只计时真正的业务处理。放外层的话，测出来的耗时
  含中间件开销，「这个指令慢」就无法归因。
- **ChannelSync 最内** —— 频道帖没有"用户"也没有"配额"，不该过限流和权限；但同步
  耗时要被计时，所以在 Observability 内侧。

## 增量同步：第二条数据入口

频道发新帖 / 编辑旧帖时，`ChannelSyncMiddleware` 把它写进索引：

```
channel_post / edited_channel_post
  │
  ▼ ChannelSyncMiddleware（不是 handler —— 频道帖没有 @command 元数据）
ChannelSync.accepts()  ── chat.id 不对 ──→ 丢弃（讨论群的转发副本，链接已失效）
  │
  ▼ message_to_export_shape()   把 aiogram Message 转成导出 JSON 的形状
  │                             UTF-16 偏移用 entity.extract_from() 换算
  ▼ parse_message()             ← 与回填**完全相同**的解析器
  ▼ upsert_many()               ← 与回填**完全相同**的写入
  ▼ mark_seen()                 双水位：看到了什么 / 存了什么
```

**两条入口共用解析器是这个设计的核心。** 两份解析代码必然漂移 —— 回填修的
bug 增量里还在，而黄金回归测试只覆盖回填那条路。`test_roundtrip_matches_export`
拿 300 条真实消息反向构造成 aiogram Message，逐字段断言两条路径结果相同。

设计过程和实测发现见 [design/incremental-sync.md](design/incremental-sync.md)，
日常契约看 [modules/ingest.md](modules/ingest.md)。

## 关键设计：装饰器只登记元数据

```python
@command("bgm", desc="查 Bangumi", rate=(3, 10))
async def cmd_bgm(message: Message, bgm_client: HttpClient) -> None:
    ...
```

`@command` **不包裹函数** —— 它只往函数上贴一个 `CommandSpec` 属性，
调用行为完全不变（[test_core.py](../tests/test_core.py) 里有测试专门验证这点）。

理由见 [ADR-0001](ADR/0001-metadata-only-decorator.md)。一句话：如果把计时、埋点、
限流做成叠加的装饰器，写新指令的人漏一个就少一层保护，而且多层包装后栈追踪没法看。
现在保护由中间件统一做一次，漏不掉。

## 模块加载

`app.py` 不认识任何具体模块名。它读 `settings.features`，然后：

```
importlib.import_module(f"animebot.features.{name}")  →  取模块的 FEATURE  →  实例化
  → container.require(*feature.requires)   缺依赖启动期就炸
  → await feature.setup()                  建连接、预热
  → feature.router()                       @command 在这一步登记进 registry
```

注意 `router()` 必须在 `with_bot=False` 时也调用 —— `@command` 是在 `bind_commands`
里才进 registry 的，不走这一步 registry 就是空的。这是个踩过的坑：
`animebot commands` 一开始输出「共 0 条指令」。

## 依赖注入

不引入 DI 框架，就是一个带生命周期的字典（[container.py](../src/animebot/core/container.py)）。

```
build_container()  →  put("repo", ...)  put("search", ...)  put("registry", ...)
                          │
                          ▼
              dp["repo"] = container.get("repo")
                          │
                          ▼ aiogram 把 workflow_data 当 kwargs 传给 handler
              async def cmd_search(message, search, settings, trace)
                                            └─ 参数名匹配容器键，自动拿到
```

为什么不用全局变量：测试里要换成假的。为什么不用 DI 框架：这个规模下框架的
启动顺序魔法比手写 20 行更难调试。

关停是**逆序**的，且单个 closer 失败不影响其它 —— 一个坏连接不能卡死整个关停。

## 可观测性三件套

| 组件 | 干什么 | 关键设计 |
|---|---|---|
| [context.py](../src/animebot/observability/context.py) | `RequestContext` 存 trace_id / user_id / command | 用 `contextvars` 而不是层层传参。asyncio 里天然按 Task 隔离，并发的两个 update 不串味（有测试验证） |
| [logging.py](../src/animebot/observability/logging.py) | structlog，控制台给人看 / 文件 JSON 给机器看 | trace_id 由 processor 自动注入，业务代码写 `log.info("bgm.hit", subject_id=123)` 就够了，不会有人忘记传 |
| [metrics.py](../src/animebot/observability/metrics.py) | 进程内计数器 + 直方图 | 刻意做得很小，够回答「哪个指令在被用、哪个报错、哪个慢」。`snapshot()` 留了导出口 |

日志里 `token` / `api_key` / `password` 这类键自动打码成 `***`。

## 错误分类

只有两类，边界必须清楚（[errors.py](../src/animebot/core/errors.py)）：

```
BotError
├── UserError ──────────────── 用户能修正的。原文给用户，日志 info
│   ├── UsageError            参数错，附用法
│   ├── PermissionDenied      没权限
│   ├── RateLimited           太频繁，带 retry_after
│   └── NotFound              没找到
├── ExternalServiceError ───── 外部 API 挂了。细节进日志，用户只看到「暂时用不了」
└── ConfigError ───────────── 配置错。启动期就炸，不拖到运行时

其它一切 Exception ─────────── 我们的 bug。用户拿 trace_id，完整栈进日志
```

这条边界不划清楚，最后一定是「要么把栈追踪喷给用户，要么把真 bug 咽掉」。

## 数据流

```
Telegram Desktop 导出 result.json          频道发新帖 / 编辑
        │                                          │
        ▼ ingest/export_loader.py                  ▼ ChannelSyncMiddleware
   频道 id 校验 ──── 不符 → 拒绝            chat.id 校验 ──── 不符 → 丢弃
        │           （讨论群的帖子是转发副本、链接失效）    │
        │                                          ▼ ingest/update_adapter.py
        │                                     Message → 导出 JSON 形状
        │                                     （UTF-16 偏移换算）
        └──────────────┬───────────────────────────┘
                       ▼ parsing/post_parser.py
              按行切 key → 查别名表 → Post
                       │      时间统一 aware UTC；未知字段进 extra 永不丢
                       ▼ storage/repo.py
              upsert posts + 重建 post_tags（+ 同步侧记水位）
                       │      主键 (channel_id, message_id)，天然幂等
                       ▼
                 SQLite (WAL)
                       │
                       ▼ search/service.py
              LIKE 预筛 → rank 打分 → rapidfuzz 兜底 → 切页
                       │
                       ▼ search/presenter.py
              渲染 + 高亮 + 键盘（callback_data 只放 id 和页码）
```

两条入口在 `parse_message` 处汇合，之后完全共用。

## 目录到职责

```
src/animebot/
├── config.py            集中配置。所有环境相关的东西只出现一次
├── cli.py               ingest / stats / syncstat / search / commands / run
├── _util.py             没有归属的小工具（指令参数提取、时长格式化）
├── core/                内核，不依赖 aiogram
│   ├── registry.py      @command + CommandRegistry
│   ├── container.py     依赖容器
│   ├── feature.py       模块协议 BaseFeature
│   ├── errors.py        错误分类
│   └── http.py          外部 API 基类（超时/重试/缓存/埋点）
├── observability/       trace、日志、埋点
├── domain/              Post 模型 + 字段别名表
├── parsing/             entity 拍平、按行切 key、解析成 Post（不依赖 aiogram）
├── storage/             schema.sql + 异步仓储
├── search/              检索服务 + 展示层 + 分页编解码 + 高亮
├── ingest/              回填、增量同步、Message→导出形状的转换
├── bot/                 aiogram 装配：app / middlewares / wiring / loader / runner / preflight
└── features/            业务模块，每个一个包
```

## 静默失败的防线

这类故障的共同点：**进程活着、指令能用、日志干净，只有某个功能悄悄不工作。**
比崩溃更难发现，所以每一条都要有主动检测。

| 故障 | 防线 | 状态 |
|---|---|---|
| bot 不是频道管理员 → 收不到 `channel_post` | `preflight()` 启动自检 + `/health` | ✅ |
| `resolve_used_update_types()` 漏掉 `channel_post` | `resolve_update_types()` 显式补，有测试 | ✅ |
| 同步停了但没人发现 | `sync_state` 双水位 + `/syncstat` | ✅ |
| 频道模板变了 | `sync.parse_failed` 埋点应始终为 0 | ✅ |
| 两条数据入口解析结果漂移 | `test_roundtrip_matches_export` | ✅ |
| 两个进程同用一个 token（409 各拿一半更新） | 单实例锁 + 409 快速失败 | ❌ [ADR-0009](ADR/0009-single-instance-lock.md) |

最后那条是已知缺口：`TelegramConflictError` 从 polling 循环抛出，不经过 handler
链，`ErrorBoundary` 抓不到（aiogram issue #1115）。待部署时补。

## 分模块细节

各层的契约、边界、坑，见 [modules/](modules/)。
