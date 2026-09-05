# ingest —— 回填与增量同步

数据进库的唯一入口。两条路径，共用同一个解析器和同一条 upsert。

| 路径 | 来源 | 时机 | 状态 |
|---|---|---|---|
| 全量回填 | Telegram Desktop 导出的 `result.json` | 手动，可重跑 | 已实现 |
| 增量同步 | `channel_post` / `edited_channel_post` update | bot 在线时自动 | 待实现 |

## 为什么必须有两条路径

**Bot API 读不到频道历史消息。** 没有 `getChatHistory`，没有 `searchMessages`
—— 那些是 MTProto（用户账号）的能力。Bot 只能收到它加入频道之后的新
`channel_post`。

那 1639 个已发布的帖子，对 bot 来说等于不存在。所以历史靠导出文件灌一次，
之后靠增量维护。详见 [ADR-0002](../ADR/0002-export-json-as-backfill-source.md)。

## 频道 id 校验：默认拒绝而不是警告

```
ExportMismatch: 导出文件是 '动漫🍵馆' (type=public_supergroup, id=1213081688)，
但配置的频道 id 是 1702674582。如果这是讨论群的导出，里面的帖子都是频道帖
的转发副本、链接已失效，不要灌进来。
```

这个错**真实发生过** —— 第一次导出导的是讨论群。讨论群里的帖子是频道帖的
自动转发副本，`forwarded_from` 指回频道，而里面的链接是失效的（帖子正文自己
就写着「请不要在讨论中打开链接，讨论中的链接是失效的」）。

灌进去的后果：索引里全是死链，而且搜索结果指向讨论群而不是频道。所以是
**拒绝**，不是警告 —— 警告会被忽略，而这个错的代价是整库污染。

两道校验：
1. `doc["id"]` 必须等于配置的 `channel_id`
2. `doc["type"]` 必须含 `channel`（挡住 supergroup）

## 幂等来自主键

主键是 `(channel_id, message_id)`，upsert 天然幂等。同一份导出灌 10 次
结果完全一样。

这条性质让两件事变简单：
- 导出可以随时重导重灌，不需要「增量导出」
- 增量同步和回填共用 `repo.upsert_many()`，不需要判断「这条是新的还是旧的」

## 每次跑都留审计

`ingest_runs` 表记录 `total/ok/partial/failed/skipped/upserted`。
`finish_run` 在 `finally` 里 —— 中途崩了也留记录，否则最需要审计的那次
反而没有。

用途：改了解析器之后对比成功率。没有这张表，「上次改完之后成功率掉了」
只能靠人肉记忆。

## 当前基线（1639 个真实帖子）

```
total=2905  ok=1504  partial=135  failed=0  skipped=1266
title_cn 100%  ·  links 98.4%  ·  tags 97.4%  ·  summary 96.6%
```

`skipped=1266` 是正常的：那些是公告、闲聊、service 消息，不是番剧帖。

`failed=0` 是红线。[test_parser_golden.py](../../tests/test_parser_golden.py)
拿真实导出跑，`failed != 0` 直接失败。

## 增量同步的设计

见 [design/incremental-sync.md](../design/incremental-sync.md)。

要点预告：`edited_channel_post` 必须和 `channel_post` 一样处理 —— 真实数据里
**每一条帖子都被编辑过**，只监听新帖会让索引永远停留在第一版内容。

## 重解析不需要重导出

`raw_text` 全量保留。改了解析器之后：

```bash
uv run python -m animebot reparse
```

从库里的 `raw_text` 重跑，不碰导出文件。**这是保留 `raw_text` 的主要理由**
—— 它让解析器可以持续演进，而不是「导出一次就定型」。
