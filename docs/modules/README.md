# 模块契约

每份文档回答三个问题：**这层负责什么、它的边界在哪、有什么坑**。

不重复代码能说清的东西（函数签名、字段列表）—— 那些看代码和 docstring。

## 依赖顺序（下层不知道上层存在）

| 文档 | 层 | 能 import 谁 |
|---|---|---|
| [core.md](core.md) | 内核 | 只有 `config` 和标准库。**不 import aiogram** |
| [observability.md](observability.md) | 内核 | 只有 `config` 和标准库 |
| [domain.md](domain.md) | 领域 | 谁都不 import |
| [parsing.md](parsing.md) | 领域 | `domain` |
| [storage.md](storage.md) | 领域 | `domain` |
| [search.md](search.md) | 应用 | `domain` / `storage` / `config` |
| [ingest.md](ingest.md) | 应用 | `domain` / `parsing` / `storage` |
| [bot.md](bot.md) | 应用 | 除 `features` 外都可以 |
| —（见 [adding_a_feature.md](../adding_a_feature.md)） | 业务 | 都可以，但 **features 之间不互相 import** |

违反这个顺序的 import 是设计错误，不是权衡。
