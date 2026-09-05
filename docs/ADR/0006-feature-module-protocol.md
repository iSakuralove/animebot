# 0006. 功能模块按名字动态加载

状态: 已采纳
日期: 2026-09-05

## 背景

规划中的模块：`search`（已有）、`bgm`（查 Bangumi）、`agent`（LLM 问答），
以后还会加。「扩展性好」这句话必须落到一个可验证的标准上，否则是空话。

选的标准是：**加一个模块，需要改几个已有文件？**

常见做法都做不到 0：

```python
# app.py 里硬编码
from .features import search, bgm, agent
for f in (search.FEATURE, bgm.FEATURE, agent.FEATURE):   # 加模块要改这里
    ...
```

```python
# 或者用 setuptools entry_points
# 每次加模块要重新 pip install -e，开发期很烦
```

## 决策

**约定优于配置 + `importlib` 按名字加载。**

```
animebot.features.<name>  必须导出 FEATURE = <BaseFeature 子类>
```

启用哪些模块由 `ANIMEBOT_FEATURES=system,search,bgm` 决定。
`app.py` 不认识任何具体模块名。

加模块的完整代价：**建一个包 + 往环境变量加一个名字。改 0 个已有文件。**

## 理由

**`app.py` 里出现 `bgm` 这个词，就意味着装配层知道了业务。** 那么加第 10 个模块时，
`app.py` 里会有 10 个 import 和 10 个条目 —— 它变成了一个必须随业务增长而修改的文件。
这类文件是合并冲突和「忘记注册」的常驻发源地。

**启动期校验依赖，而不是运行时。**

```python
class BgmFeature(BaseFeature):
    requires = ("registry", "http")
```

`container.require(*feature.requires)` 在加载时就跑。缺依赖的表现是**启动失败**
并列出已注册的键，而不是用户敲 `/bgm` 时才 KeyError。

**生命周期钩子成对。** `setup()` 建连接、预热；`teardown()` 关。容器逆序关闭，
且单个 closer 失败不影响其它 —— 一个坏连接不能卡死整个关停（有测试验证）。

**`system` 模块自己也走这套。** 它没有特权，`/help`、`/health`、`/metrics` 和
`search` 用完全相同的机制装配。这是对「协议够不够用」唯一诚实的检验 —— 如果内置
功能需要走后门，那协议就是不够用的。

## 代价

**动态 import 让静态分析看不到调用链。** IDE 不会告诉你 `features/bgm/__init__.py`
被谁用了。缓解手段是 `animebot commands` —— 不连 Telegram 就能列出所有已装配指令，
这是比静态分析更直接的验证。

**模块名拼错要到启动时才知道。** `ConfigError` 会明确说「找不到
`animebot.features.bgmm`」，不是 ImportError 栈。

**`router()` 必须在不起 bot 时也调用。** 这是个踩过的坑：`@command` 是在
`bind_commands()` 里才登记进 registry 的，所以 `with_bot=False` 时如果跳过
`router()`，registry 就是空的 —— `animebot commands` 一开始输出「共 0 条指令」。
现在 `build_app()` 里无条件调用 `router()`，只是 bot 为 None 时不 include 进
Dispatcher。

## 验证

```bash
uv run animebot commands
```

```
[system]
  /help (/start, /h)      查看所有指令
  /ping  R   测活
  ...
[search]
  /search (/s, /find)  R   搜索番剧
  ...
共 8 条指令
```

`A`=管理员 `H`=隐藏 `R`=有限流。加模块后跑这个，能一次看出：指令登记成功没有、
别名冲突没有、权限标记对不对。重名指令会在 `CommandRegistry.add()` 里抛
`ConfigError` —— 启动期就炸，不会变成「运行时随机命中一个」。

## 相关

- [adding_a_feature.md](../adding_a_feature.md) —— 操作手册
- [modules/bot.md](../modules/bot.md) —— loader 与 wiring 的契约
