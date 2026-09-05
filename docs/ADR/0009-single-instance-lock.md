# 0009. 单实例锁 + 409 快速失败

状态: 已接受，待实现（排在增量同步之后）
日期: 2026-09-05

## 背景

调研「有什么现成项目能探测 bot 可用性」时发现，整个监控生态漏掉了对
long polling bot 最致命的一个失败模式。

**Telegram 只允许一个 token 有一条长轮询连接。** 第二个进程连上来，服务器把
第一个踢掉并返回 409：

```json
{"ok":false,"error_code":409,
 "description":"Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"}
```

第一个立刻重连，又把第二个踢掉。结果是**两个实例各自随机拿到大约一半的更新**。

这时候所有常规探针都是绿的：

| 探测手段 | 状态 |
|---|---|
| 进程心跳（healthchecks / Uptime Kuma push） | 🟢 进程活着 |
| `getMe` | 🟢 token 有效 |
| HTTP / TCP 层探测 | 🟢 没问题 |
| **实际行为** | 🔴 **一半的消息不回** |

比崩溃更糟。崩溃是响亮的，这个是安静的。

**而且 aiogram 的 error handler 抓不到它。** `TelegramConflictError` 从 polling
循环里抛出，不经过 handler 链 —— 我们那套 `ErrorBoundary` 中间件对它完全无感。
aiogram issue #1115 里维护者明确说了：error handler 只能处理来自 event handler
的异常。

对这个项目来说概率很高：本地调试时开着，服务器上也在跑，就中了。
增量同步要求 bot 长期在线，恰恰是最容易出现两个实例的场景。

## 决策

**不加探针去检测它，而是让它不可能发生。**

1. **启动时抢单实例锁。** 抢不到就打印「已经有一个实例在跑，PID xxx」并非零退出。
2. **捕获 409 后立即非零退出，绝不重试。**

## 理由

> 有时你可以换个角度看问题，重写它让特殊情况消失，变成正常情况。

加一层探针去检测 409，是在给一个本该不存在的状态写补丁。锁让这个状态从
「可能发生，需要检测」变成「不可能发生」—— 特殊情况消失了。

**409 重试是错的。** Telegraf 社区在争论要加 `retryOnConflict`
（issue #2084，已合并为可选项）。对滚动部署有意义 —— 旧进程的连接还没超时，
新进程等一会儿就好。但对单实例部署，重试意味着**接受**「两个实例互相踢、各拿一半」
这个状态，那正是我们要消灭的东西。死掉是正确行为：进程管理器会重启它，
而如果真有第二个实例，重启后照样死，日志里会有明确的 409 —— 响亮的失败。

**锁的实现选文件锁**（`msvcrt.locking` / `fcntl.flock`），不用 PID 文件：
PID 文件在进程被 SIGKILL 后会留下陈旧内容，得额外判断「这个 PID 还活着吗」，
而 PID 会被复用。文件锁由操作系统在进程退出时自动释放，没有这个问题。

## 代价

**跨机器无效。** 文件锁只保护同一台机器。真正的多机场景需要分布式锁
（Redis / etcd）或者换 webhook。当前是单机部署，不需要。GLM 提到的
「服务器 A 挂了切到 B，A 又活了」这个场景确实存在，但那时候的正解是
**revoke token 换新的**，而不是让两个实例协商 —— 后者是分布式共识问题，
不该在一个单机 bot 里解决。

**开发体验略差。** 本地想同时开两个实例调试会被拦。这正是想要的效果。
真需要的话用不同的 token（BotFather 建个 dev bot），这本来就是正确做法
—— 用同一个 token 开发和生产是 409 最常见的成因。

## 实现要点（待做）

```
bot/single_instance.py
  acquire(lock_path) -> 拿不到就 ConfigError，消息里带持锁进程的 PID
  跟 App 的生命周期绑：build_app 里 acquire，aclose 里 release

bot/runner.py
  except TelegramConflictError:
      log.critical("bot.conflict", ...)   # 明确说「有另一个实例在用这个 token」
      return 2                            # 非零退出，不重试
```

放在 `runner.py` 而不是中间件里 —— 因为它压根不经过中间件。

## 相关

- [operations.md](../operations.md) —— 监控整体方案
- 调研结论：`getWebhookInfo` 那条路对 long polling 无效（`url` 是空串，
  没有投递记录可看），所以监控栈从四层变三层
