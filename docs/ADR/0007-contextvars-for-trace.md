# 0007. 用 contextvars 传 trace 上下文

状态: 已采纳
日期: 2026-09-05

## 背景

用户报错时说「出错了」，要能定位到具体是哪次请求。所以需要一个 trace_id
贯穿：中间件 → handler → service → HTTP 客户端 → SQL 层，每条日志都带上它。

一次请求要穿 5~6 层调用栈。备选：

| 方式 | 问题 |
|---|---|
| 每层多一个 `ctx` 参数 | 5~6 层全要改签名；异步任务里容易传丢；纯噪音 |
| 全局变量 | 并发的两个 update 会串味 |
| `threading.local` | asyncio 单线程跑多个 Task，完全无效 |
| `contextvars` | ✅ |

## 决策

`RequestContext` 存在 `contextvars.ContextVar` 里。structlog 的 processor
从中读出来自动注入每条日志。

业务代码写：

```python
log.info("bgm.hit", subject_id=123)      # trace_id 自动带上
bind(keyword=kw)                          # 给当前上下文补字段
```

## 理由

**contextvars 在 asyncio 里天然按 Task 隔离。** 每个 `asyncio.Task` 有自己的
Context 副本，`aiogram` 的 `handle_as_tasks=True`（默认）给每个 update 开一个
Task，所以并发的两个请求不会串。有测试验证：

```python
async def test_isolated_across_tasks(self) -> None:
    """并发的两个 update 不能串 trace"""
    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert len(set(seen)) == 3
```

**自动注入 = 不会有人忘记传。** 如果 trace_id 要手写进每个 `log.info` 调用，
那必然有人漏。processor 注入是结构性的保证。

**8 位 trace_id 够用，而且用户能复述。** 日志按天切分，同一天内 uuid4 前 8 位
碰撞概率可以忽略。用户报「错误编号 a1b2c3d4」，`grep a1b2c3d4 logs/animebot.jsonl`
直接定位。32 位的完整 uuid 用户会抄错。

**`bind()` 在没有上下文时静默忽略。** CLI 和测试里没有 update，也没有 trace
上下文。让 `bind()` 在这种情况下抛异常会强迫所有调用点写 if，得不偿失。

## 代价

**看代码看不出日志里会有哪些字段。** `log.info("search.done")` 实际输出会带
trace_id / user_id / chat_id / command / feature。缓解：这些字段的来源集中在
`RequestContext.as_log_fields()` 一个方法里。

**跨进程边界会丢。** 如果以后引入 worker 进程或消息队列，trace_id 要显式塞进
消息体。目前单进程，不是问题。

**用 `ContextVar` 存可变对象需要小心。** `RequestContext` 是可变的
（`bind()` 会改它），如果一个 Task 把 context 传给了子 Task，两者会共享同一个对象。
目前只有中间件创建它、handler 读它，没有跨 Task 传递的场景。

## 顺带：日志分双通道

同一份日志，两个渲染器：

- **控制台** → `ConsoleRenderer`，带颜色，给人看
- **文件** → `JSONRenderer` + `dict_tracebacks`，JSON Lines，一天一个文件，留 14 天

理由：开发时要人眼可读，出事故时要能 `jq` 过滤。一个格式满足不了两个用途。

`token` / `api_key` / `password` / `authorization` 这类键在两个通道都自动打码成
`***` —— 这是 processor 层做的，不指望每个调用点自觉。

## 相关

- [modules/observability.md](../modules/observability.md) —— 三件套的完整契约
