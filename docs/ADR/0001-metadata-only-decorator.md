# 0001. 装饰器只登记元数据，行为放中间件

状态: 已采纳
日期: 2026-09-05

## 背景

要给每个指令加上：计时、埋点、错误边界、限流、权限校验、trace 注入。

Python 生态的直觉做法是叠装饰器：

```python
@command("search")
@rate_limit(6, 20)
@track_metrics
@catch_errors
@require_trace
async def cmd_search(...): ...
```

后续要加 `/bgm`、`/agent` 等一批模块，每个模块作者都要正确地把这五个装饰器
按正确顺序贴上去。

## 决策

`@command` **只往函数上贴一个 `CommandSpec` 属性**，不包裹函数。所有行为
由 aiogram 中间件链统一执行一次。

限流参数这类元数据写在 `@command(rate=(6, 20))` 里，由
`RateLimitMiddleware` 从注册表读出来执行。

## 理由

**漏一个就少一层保护。** 叠装饰器的方案里，「新指令自动获得全部保护」不成立
—— 它依赖每个作者记得贴全。中间件方案里，指令只要被 Router 接住就必然经过全链，
不存在漏的可能。

**栈追踪。** 五层装饰器嵌套后，真异常的栈里有五层无关的 wrapper 帧。
`ErrorBoundary` 里 `log.exception` 打出来的栈要能直接定位到业务代码那一行。

**顺序敏感性从代码转移到了一处配置。** 装饰器的顺序等于嵌套顺序，写错了很难发现。
现在顺序只在 `_build_dispatcher()` 里出现一次，而且带注释解释了为什么是这个顺序。

**元数据可以被别的东西复用。** `/help`、`setMyCommands`、`animebot commands`
都从同一张注册表读。如果限流参数藏在装饰器闭包里，`/help` 就没法显示「10 秒内 3 次」。

## 代价

**多一层间接。** 看 `cmd_search` 的代码看不出它有限流 —— 得看 `@command` 的参数。
缓解手段：参数就写在紧贴函数的装饰器里，视觉距离最近。

**中间件拿不到 handler 的返回值语义。** 现在中间件只能按异常类型分类，
不能按返回值做决策。目前不需要。

**这个决策在什么时候失效**：如果出现「只有某几个指令需要的、且无法用元数据描述的
行为」（比如某个指令要走完全不同的重试策略），那它应该在 handler 内部显式做，
而不是硬塞进中间件链。中间件只放**所有指令共享**的东西。

## 验证

[test_core.py](../../tests/test_core.py) 有一条 `test_does_not_wrap_function`
专门断言被装饰的函数调用行为不变。

[test_middlewares.py](../../tests/test_middlewares.py) 手搓 `Update` 喂进真
Dispatcher，验证保护链真的生效：崩溃时用户拿到 trace_id 而不是
`ZeroDivisionError`、限流卡在第 3 次、`admin_only` 挡住普通用户。
