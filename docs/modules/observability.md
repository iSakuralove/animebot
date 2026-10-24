# observability —— 可观测性

`src/animebot/observability/`

## 职责

回答三个问题：

- **这次请求发生了什么** → `context.py` + `logging.py`（trace_id 串起全链路）
- **系统整体在干什么** → `metrics.py`（哪个指令在被用、哪个报错、哪个慢）
- **出事故了怎么定位** → JSON 日志 + 8 位 trace_id，`grep` 就够

## context —— 请求上下文

`RequestContext` 存在 `contextvars.ContextVar` 里。设计理由见
[ADR-0007](../ADR/0007-contextvars-for-trace.md)。

### 契约

| API | 用途 |
|---|---|
| `current()` | 拿当前上下文，没有返回 `None` |
| `trace_id()` | 拿 trace_id，没有返回 `"-"` |
| `bind(**kw)` | 给当前上下文补字段。**没有上下文时静默忽略** |
| `set_context` / `reset_context` | 中间件用，配对使用 |
| `request_context(**kw)` | 上下文管理器，CLI / 定时任务 / 测试用 |

`bind()` 的行为：字段名在 `RequestContext` 上存在就直接设，否则进 `extra` dict。
两种都会出现在后续日志里。

### 坑

**`bind()` 静默忽略是刻意的。** CLI 和测试里没有 update，也没有 trace 上下文。
让它抛异常会强迫所有调用点写 if，得不偿失。

**`RequestContext` 是可变对象。** 如果一个 Task 把它传给子 Task，两者共享同一个实例。
目前只有中间件创建、handler 读，没有跨 Task 传递的场景。真要在子 Task 里用，
应该 `contextvars.copy_context()`。

**并发隔离有测试守着。** `test_isolated_across_tasks` 用 `asyncio.gather` 跑三个
worker，断言三个 trace_id 互不相同、各自的 `command` 没被串改。

## logging —— 结构化日志

### 双通道

同一份日志，两个渲染器：

| 通道 | 渲染器 | 给谁看 |
|---|---|---|
| stderr | `ConsoleRenderer`（带颜色） | 人 |
| `logs/animebot.jsonl` | `JSONRenderer` + `dict_tracebacks` | 机器（`jq` / grep） |

文件一天一个，留 14 天（`TimedRotatingFileHandler`）。

开发时要人眼可读，出事故时要能过滤 —— 一个格式满足不了两个用途。

### 自动注入的东西

processor 链会往每条日志加：

```
trace_id, update_id, user_id, username, chat_id, chat_type, feature, command
+ bind() 塞进 extra 的一切
+ timestamp（ISO，本地时区）, level, logger 名
```

所以业务代码写 `log.info("bgm.hit", subject_id=123)` 就够了。**不用手传 trace_id**
—— 这不是省事，是保证：手传就必然有人漏。

### 密钥打码

`token` / `bot_token` / `api_key` / `password` / `authorization` 这些键的值
自动变 `***`。在 processor 层做，不指望每个调用点自觉。

### 契约

- `setup_logging()` **幂等**。多次调用只生效第一次，测试里反复 setup 不会叠加 handler
- `get_logger(name)` 返回 `structlog.stdlib.BoundLogger`
- `aiogram.event` / `aiohttp.access` / `asyncio` 被压到 WARNING —— 它们的 INFO 太吵
  （aiogram 自己那条「Update id=… is handled」被我们的 `handler.done` 取代）
- `aiogram.dispatcher` 上挂 `_TransientPollingFilter`：polling 遇到
  `TelegramNetworkError` / `TelegramServerError` / `TelegramRetryAfter` 时那条
  「Failed to fetch updates」从 ERROR 降为 WARNING。409 冲突、token 失效照旧 ERROR。
  见 [ADR-0014](../ADR/0014-access-log-one-line-per-request.md)

### 访问日志：一次交互一行

`ObservabilityMiddleware` 给每个**被 handler 处理**的 update 打一条 INFO：

```
handler.done  command=c outcome=ok elapsed_ms=12.4 payload=进击 trace_id=ab12cd34
              user_id=… username=… chat_type=private feature=manga
              scope=manga query=进击 mode=lenient result_count=1 page=0 reason=prefix
```

- 前四个字段中间件自己给；trace 字段由 processor 注入；后面的业务字段是 handler
  `bind()` 进去的。**handler 不需要自己打日志**，bind 就会出现在这一行里。
- 超过 `slow_command_ms` 换成 WARNING `handler.slow`（同样的字段），仍然一行。
- 没有 handler 接的 update（频道帖、群里没触发指令的消息）只打 DEBUG
  `handler.unhandled`。频道帖由同步自己打 `sync.*`。
- `command` / `feature` 在 handler **之后**取：回调按钮的 command 是 `cb`、纯文本
  是 `message`，它们属于哪个模块由 handler `bind(feature=...)` 报上来。

搜索类 handler 约定 bind 的字段：

| 字段 | 值 |
|---|---|
| `scope` | `anime` / `manga` |
| `query` | 查询词（截 80 字） |
| `mode` | `strict`（/s）/ `lenient`（纯文本、/c） |
| `result_count` | 命中总数（不是本页条数） |
| `page` | 0-based |
| `reason` | 第一条的命中层级：exact / prefix / title / alias / fuzzy / tag / none |

### 事件命名约定

`<域>.<动作>`，全小写点分：

```
handler.done      handler.slow       handler.unhandled  handler.crash   handler.user_error
http.ok           http.timeout       http.retryable     http.cache_hit
feature.loaded    command.bound      app.ready          app.wired
bot.start         bot.stopping       bot.commands_synced
preflight.channel_ok   preflight.channel_unreachable   preflight.username_mismatch
sync.upserted     sync.file          sync.not_post      sync.parse_failed
manga.ready       manga.empty
```

用固定的事件名 + 结构化字段，而不是把变量拼进消息串
—— 前者能 `grep 'handler.crash'` 精确匹配，后者只能模糊搜。

## metrics —— 埋点

进程内聚合，`/metrics` 指令直接读，不依赖外部 Prometheus。

### 两种类型，够用

| 类型 | API | 用途 |
|---|---|---|
| 计数器 | `METRICS.incr(name, value=1, **labels)` | 调用数、错误数、缓存命中 |
| 直方图 | `METRICS.observe(name, ms, **labels)` / `METRICS.timer(name)` | 延迟分布 |

标签编码成 `name{k=v,k2=v2}`（key 排序，`None` 值跳过），
所以 `snapshot()` 出来是个扁平 dict，直接能渲染也能导出。

### 分桶

```
50, 100, 250, 500, 1000, 2500, 5000, 10000 ms
```

分界点照真实体感取：100ms 内无感，1s 开始有感，5s 是能忍的上限。

分位数是桶内线性近似，**不精确**。这是刻意的：它的用途是「发现变慢了」，
不是 SLA 报告。要精确分位数得存全量样本，那是另一个数量级的成本。

### 中间件已经埋了什么

新模块**不需要重复埋**这些：

```
handler.calls{command,feature,outcome}     outcome = ok | user_error | error
handler.latency{command,feature}
handler.crash{error}
handler.rate_limited{command}
```

模块自己只埋**业务语义**的点 —— 中间件不可能知道的东西：

```python
METRICS.incr("bgm.search", found=bool(items))    # 查到了没有
METRICS.observe("bgm.results", len(items))       # 返回了几条
bind(subject_id=items[0]["id"])                  # 这次请求的业务主键
```

### 契约

- 线程安全（有锁）。aiogram 单线程，但 CLI/测试可能多线程，锁的成本可忽略
- `snapshot()` 返回可 JSON 化的 dict，留了导出到 Prometheus 的口
- `reset()` 清空 —— 测试用

## 排障怎么用这套

用户说「出错了，编号 a1b2c3d4」：

```bash
grep a1b2c3d4 logs/animebot.jsonl
```

一次请求的所有日志都带同一个 trace_id，包括完整栈追踪（`dict_tracebacks` 展开成
结构化字段，不是一坨字符串）。

用户想要、库里没有的东西（最该补的资源清单）：

```bash
journalctl -u animebot --since "7 days ago" | grep handler.done | grep "result_count=0"
jq -r 'select(.event=="handler.done" and .result_count==0) | [.scope,.query] | @tsv' \
   logs/animebot.jsonl* | sort | uniq -c | sort -rn | head -30
```

想看哪个指令慢：`/metrics` 里 `handler.latency{command=...}` 的 p95，或者
`grep handler.slow`。

想看哪个指令在报错：`handler.calls{...,outcome=error}` 的计数。

## 测试

[test_core.py](../../tests/test_core.py) 里的 `TestRequestContext` 和 `TestMetrics`。
中间件层的埋点集成测试在 [test_middlewares.py](../../tests/test_middlewares.py)
的 `TestMetricsIntegration`。访问日志和 polling 降级在
[test_access_log.py](../../tests/test_access_log.py)：用 `structlog.testing.capture_logs`
带上 `_inject_trace` 跑真的中间件链，断言「一次交互正好一行、业务字段都在」。
