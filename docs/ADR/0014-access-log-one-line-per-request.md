# 0014. 每次交互一条 INFO 访问日志；polling 的瞬时网络错误降为 WARNING

状态: 已采纳
日期: 2026-10-24

## 背景

线上（VPS，`ANIMEBOT_LOG_LEVEL=INFO`）2026-10-23 ~ 24 的 `journalctl` 实测：

- **用户的任何操作都不产生日志。** 唯一记录每次请求的 `handler.done` 是 DEBUG；
  INFO 及以上只有启动那几行，加上慢查询和崩溃。「谁搜了什么、搜到没有、为什么
  慢」全靠猜。
- **ERROR 全是 aiogram 的 polling 自愈噪音。** `Failed to fetch updates -
  TelegramNetworkError: … Connection reset by peer`、`TelegramServerError: Bad
  Gateway`，每天好几次，几秒内 `Connection established` 自己恢复。那段时间里
  ERROR 行 100% 是它们 —— 真正的 `handler.crash` 会被淹没在 `grep error` 里。

而验证流程是「部署到 VPS、看 journalctl」（AGENTS.md），日志就是唯一的窗口。

## 决策

1. **`ObservabilityMiddleware` 给每个被 handler 处理的 update 打一条 INFO
   `handler.done`**：`command`、`feature`、`outcome`、`elapsed_ms`、`payload`，
   加上 trace 上下文（trace_id、user_id、username、chat…）和 handler 用 `bind()`
   补的业务字段。超过 `slow_command_ms` 的换成 WARNING `handler.slow`，仍然是一行。
   没有 handler 接的 update（频道帖、没触发指令的消息）只打 DEBUG。
2. **业务字段走 `bind()`，不另打日志。** 搜索类 handler 统一 bind
   `scope`（anime/manga）、`query`、`mode`（strict/lenient）、`result_count`、
   `page`、`reason`（命中层级）。
3. **`command` / `feature` 标签在 handler 之后取。** 回调按钮没有指令名，它属于
   哪个模块只有 handler 知道（`bind(feature="manga")`）；纯文本搜索同理。
4. **`aiogram.dispatcher` 上挂一个 filter**：`Failed to fetch updates` 里异常类名是
   `TelegramNetworkError` / `TelegramServerError` / `TelegramRetryAfter` 的降为
   WARNING。`TelegramConflictError`（409）、`TelegramUnauthorizedError` 照旧 ERROR。
5. **启动和同步日志带频道。** `app.ready posts={anime: …, manga: …} files={…}`、
   `manga.ready` / `manga.empty`、`preflight.* channel=…`、`sync.* channel=…`；
   新增 `sync.file`；`sync.not_post` 从 DEBUG 提到 INFO（频道帖不进访问日志，
   这是「同步还在收到东西」的唯一可见证据）。

## 理由

**一次请求一行（canonical log line）。** 所有上下文在同一行里，`grep` 一次就完：

```bash
journalctl -u animebot | grep handler.done | grep 'result_count=0'   # 用户想要但库里没有的
journalctl -u animebot | grep 'scope=manga'                          # 只看漫画
journalctl -u animebot | grep a1b2c3d4                               # 用户报的错误编号
```

第一条是这次改动最有价值的产出：**搜了但没搜到的词，就是频道该补的资源清单。**

**为什么在中间件打，不在 handler 里打。** 和 ADR-0001 同一个理由：保护（这里是
可观测性）由中间件统一做一次，写新指令的人不可能忘记。handler 只 `bind()`
它独有的业务字段。

**为什么按异常类名判，不匹配报错原文。** aiogram 用
`loggers.dispatcher.error("Failed to fetch updates - %s: %s", type(e).__name__, e)`
打这条日志，类名是格式化参数 `record.args[0]`，比解析 `Connection reset by peer`
这种因平台而异的文字可靠得多。

**为什么只降级不吞掉。** 它们仍然是「网络在抖」的真实信号，WARNING 里看得到；
只是不该和「代码崩了」放在同一个级别。

## 代价

- **用户的查询词进了日志。** user_id / username 本来就在 trace 字段里；日志按天
  切、留 14 天。
- **依赖 aiogram 这条日志的格式。** 哪天 aiogram 改了措辞，filter 不再匹配，这些
  行退回 ERROR —— 失败方向是「吵」而不是「静默」，可以接受。
- **INFO 量随使用量线性增长**，一次交互一行。按现在的使用量可以忽略；真到每天
  几万次交互时，再考虑把 NOOP 占位按钮这类点击降到 DEBUG。

## 相关

- [modules/observability.md](../modules/observability.md) —— 事件名清单与排障用法
- [ADR-0001](0001-metadata-only-decorator.md) —— 行为放中间件
- [ADR-0007](0007-contextvars-for-trace.md) —— trace 上下文
