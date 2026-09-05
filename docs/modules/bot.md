# bot —— aiogram 装配层

唯一 import aiogram 的地方。四个文件：

| 文件 | 负责 |
|---|---|
| [app.py](../../src/animebot/bot/app.py) | 启动顺序、中间件顺序、关停 |
| [loader.py](../../src/animebot/bot/loader.py) | 按名字动态 import 功能模块 |
| [wiring.py](../../src/animebot/bot/wiring.py) | `@command` → aiogram Router |
| [middlewares.py](../../src/animebot/bot/middlewares.py) | trace / 埋点 / 错误边界 / 限流 / 权限 / 频道同步 |
| [preflight.py](../../src/animebot/bot/preflight.py) | 启动自检：bot 在频道里是管理员吗 |

## 中间件顺序是设计，不是偏好

```
Trace          ← 最外。之后所有日志自动带 trace_id
  ErrorBoundary   ← 次之。下游一切异常都被兜住
    Access          ← 抛 PermissionDenied
      RateLimit       ← 抛 RateLimited
        Observability   ← 计时真正的业务处理
          ChannelSync     ← 最内。频道帖不该过限流/权限，但要被计时
            handler
```

四条约束决定了这个顺序，改动前先确认没破坏它们：

1. **Trace 必须最外** —— 它之后的任何日志都要带 trace_id，包括错误边界打的日志。
2. **Access 和 RateLimit 必须在 ErrorBoundary 内侧** —— 它们抛的是 `UserError`，
   要被 ErrorBoundary 转成用户看得懂的回复。放外侧就变成未捕获异常。
3. **Observability 在它们内侧** —— 计时不该包含中间件自己的开销，否则 p95 里
   混着限流查表的时间。
4. **ChannelSync 最内** —— 频道帖没有"用户"也没有"配额"，不该过限流和权限；
   但同步耗时要被计时，所以在 Observability 内侧。

## ErrorBoundary：唯一的兜底出口

```
UserError   → 原文给用户，日志 info
BotError    → user_message + trace_id，日志 error
其它异常     → 「内部错误，编号 a1b2c3d4」，日志 exception（含完整栈）
```

用户报「出错了，编号 a1b2c3d4」，`grep a1b2c3d4 logs/animebot.jsonl` 直接
定位到那一次请求的完整链路。这是 8 位 trace_id 的全部意义。

`_reply()` 有两条硬规矩：

1. **绝不往频道里发消息。** `channel_post` 也是 `Message` —— 不挡住的话，一次
   同步异常就会让 bot 在 3000 人的频道里公开发「内部错误，编号 xxx」。
   遇到 `ChatType.CHANNEL` 直接抑制并记 `reply.suppressed_in_channel`。
2. **回复失败不能再抛**，否则变成 aiogram 的未处理异常，栈追踪里看不到原始错误。

## `allowed_updates` 的坑

同步做成中间件，而 `dp.resolve_used_update_types()` **只扫 handler** ——
它算出来的列表里没有 `channel_post`，传给 `start_polling` 之后 Telegram 就
再也不推频道帖。同步永久静默失效，日志一片安静。

实测：只有 message handler 时返回 `['message']`；注册了 channel_post handler
才返回 `['channel_post', 'message']`。

所以 `resolve_update_types()` 显式并上 `("channel_post", "edited_channel_post")`，
[test_sync_middleware.py](../../tests/test_sync_middleware.py) 有测试盯着。

## 启动自检

`preflight()` 查 bot 在目标频道的身份。Bot API 的 `channel_post` **只推给频道
的管理员 bot** —— bot 没加进频道或被降权，Telegram 一条 update 都不推，而这个
失败完全静默。

只警告不阻断：网络抖一下就拒绝启动是把可用性换成了洁癖。结论存进
`App.channel_access`，`/health` 读它显示。

顺带校验配的 `channel_username` 和频道实际的是否一致 —— 不一致的话公开深链
会指向错误的频道或 404。

## ErrorBoundary 抓不到什么

**`TelegramConflictError`（409）抓不到。** 它从 polling 循环抛出，不经过
handler 链 —— aiogram 官方 issue #1115 里维护者明确说了这个。

后果：两个进程同用一个 token 时，Telegram 只允许一条长轮询连接，两边互相
踢、各拿一半更新。进程活着、`getMe` 正常、心跳照发，但一半消息不回。
比崩溃更难发现。

解法不是加探针，是 [ADR-0009](../ADR/0009-single-instance-lock.md)：启动抢
单实例锁，409 立即非零退出。**待实现。**

## RateLimit 是进程内的

`dict[(user_id, command)] → deque[timestamp]` 滑动窗，参数来自
`@command(rate=(3, 10))`。

单实例部署够用。每 300 秒 GC 一次死 key，防止长期运行内存增长。
多实例时换 Redis，但 `RateLimitMiddleware` 的接口不变。

## `_adapt`：按签名过滤 kwargs

aiogram 把 `data` 里所有键当 kwargs 传给 handler。不做过滤的话每个 handler
都得写 `**_kw`，或者被无关参数炸掉。

```python
async def cmd_search(msg: Message, search: SearchService) -> None:
```

声明什么就拿到什么。`dp["search"]`、`dp["repo"]`、`dp["registry"]`、
`dp["settings"]`、`dp["container"]`、`dp["app"]` 都可以按名字取。

## 启动顺序里的一个坑

```python
routers = [f.router() for f in app.features]   # ← 必须在 with_bot 判断之前
if not with_bot:
    return app
```

`@command` 是在 `router()` 被调用时才登记进 `CommandRegistry` 的。所以哪怕
不起 bot（CLI、测试），也要走一遍，否则 registry 是空的，`/help` 和
`--stats` 都会看到 0 个指令。

## 关停

`App.aclose()` 逆序 teardown 模块 → 关容器 → 关 bot session。

单个 teardown 失败只记日志不中断，避免一个坏模块卡死整个关停。
`build_app` 里任何异常都会先 `aclose()` 再重抛 —— 否则启动失败会漏一个
打开的数据库连接。

## 加指令不需要碰这一层

`sync_bot_commands()` 从 `registry.visible()` 生成 `setMyCommands`。
加一个 `@command` 就自动进 Telegram 的指令菜单，不需要去 BotFather 手动改。

怎么加模块见 [adding_a_feature.md](../adding_a_feature.md)。
