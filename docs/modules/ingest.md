# ingest —— 回填与增量同步

数据进库的唯一入口。两条路径，共用同一个解析器和同一条 upsert。

| 路径 | 来源 | 时机 |
|---|---|---|
| 全量回填 | Telegram Desktop 导出的 `result.json` | 手动，可重跑 |
| 增量同步 | `channel_post` / `edited_channel_post` update | bot 在线时自动 |

两条路径都服务所有已配置的频道（动漫、漫画，见 `Settings.channels`）：

- **回填**：`resolve_channel()` 按导出文件自己的 id 认频道，不是任何一个就拒绝。
  同一个命令 `animebot ingest <目录>` 灌动漫或漫画都行。
- **同步**：一个 `ChannelSync` 实例，按 `chat.id` 查表决定记到哪个频道名下。
- **文件消息**（文档：zip/rar/7z）两条路径都走 `parse_file()` → `upsert_files()`。
  aiogram 的 `Message.document` 由 `update_adapter` 转成导出里 `file_name` /
  `file_size` / `mime_type` 的形状。GIF 例外：Bot API 为了向后兼容给 GIF 同时填
  `animation` 和 `document`，adapter 照导出的做法标 `media_type=animation`，
  `parse_file` 据此排除。

## 为什么必须有两条路径

**Bot API 读不到频道历史消息。** 没有 `getChatHistory`，没有 `searchMessages`
—— 那些是 MTProto（用户账号）的能力。Bot 只能收到它加入频道之后的新
`channel_post`。

那 1639 个已发布的帖子，对 bot 来说等于不存在。所以历史靠导出文件灌一次，
之后靠增量维护。详见 [ADR-0002](../ADR/0002-export-json-as-backfill-source.md)。

## 共用解析器不是洁癖

两条入口如果各有一份解析代码，它们**必然漂移** —— 回填修的 bug 增量里还在，
反之亦然。而 1639 个帖子的黄金回归测试只覆盖回填那条路，第二份解析器等于零
测试覆盖。

所以 [update_adapter.py](../../src/animebot/ingest/update_adapter.py) 把
aiogram `Message` 转成**导出 JSON 的形状**，再喂给同一个 `parse_message`。
绕这一圈的成本是一个转换函数，收益是解析逻辑永远只有一份。

[test_sync.py](../../tests/test_sync.py) 里的
`test_roundtrip_matches_export` 是这个设计的守卫：拿 300 条真实导出消息反向
构造成 aiogram Message，走增量路径，逐字段断言与回填结果完全相同。

## 坑一：UTF-16 偏移

Bot API 的 entity `offset` / `length` 按 **UTF-16 码元**计，Python 字符串按
码位。帖子正文里 emoji 密集（`💙故事简介`、`🔐解压`、`😱百度网盘`），一个
emoji 占 2 个码元但 `len()` 是 1。

直接拿 offset 当 Python 索引，后面所有偏移都错位，链接归属到错误的行 ——
症状是"新帖的网盘链接少了几个"，几个月都不会有人发现。

**用 `entity.extract_from(text)`，不要自己算。** aiogram 内部已经处理了
surrogate 换算。`_utf16_len()` 只用来算空隙长度。

entity 之间的空隙必须补成 plain 段：`flatten()` 靠把所有段拼起来还原全文，
漏一段就让后续行号全错。

## 坑二：`date` 是 datetime，`edit_date` 是 int

aiogram 3.31 的字段类型不一致：

```python
Message.date       -> datetime.datetime
Message.edit_date  -> int | None        # ← 不是 datetime
Message.forward_date -> datetime | None
```

假设 `edit_date` 是 datetime 会在「被编辑过的帖子」上抛 `AttributeError`
—— 而这个频道 **1486/1486 的帖子都编辑过**，等于每一条增量都炸。

`_epoch()` 两种都接。这不是防御性编程，是对已知不一致的处理。

## 坑三：时区

导出 JSON 的 `date` 是**导出机器的本地时间**（这个频道全是 +08:00），
而 aiogram 给的是真 UTC。

拿 `date` 当 UTC 会让回填数据整体偏移 8 小时，而增量数据是准的 —— 两批数据
在 `ORDER BY posted_at` 里错开，新帖排到错误的位置。

解析器优先读 `date_unixtime`（导出里覆盖率 100%），那是无歧义的绝对时间。
`Post.posted_at` 一律是 aware UTC，两条入口在各自的解析层归一化。

## 频道 id 校验：默认拒绝而不是警告

```
ExportMismatch: 导出文件是 '动漫🍵馆' (type=public_supergroup, id=1213081688)，
但配置的频道 id 是 1702674582。
```

这个错**真实发生过** —— 第一次导出导的是讨论群。讨论群里的帖子是频道帖的
自动转发副本，而链接是失效的（帖子正文自己就写着「讨论中的链接是失效的」）。

灌进去的后果：索引里全是死链，搜索结果指向讨论群。所以是**拒绝**不是警告
—— 警告会被忽略，而这个错的代价是整库污染。

增量同步侧的同源校验是 `ChannelSync.accepts()`：`chat.id` 不等于
`bot_api_chat_id` 直接丢弃。

## 静默失败：为什么有 `sync_state` 表

**增量同步停止工作是完全静默的。** bot 被降权、被移出频道、
`allowed_updates` 漏了 `channel_post` —— 这些故障下进程活着、指令能用、
日志干净，只有索引悄悄不再更新。

三道防线：

**一、启动自检**（[preflight.py](../../src/animebot/bot/preflight.py)）。
查 bot 在频道的身份，不是管理员就打 warning。只警告不阻断 —— 网络抖一下就
拒绝启动是把可用性换成了洁癖。顺带校验配的 `channel_username` 和频道实际的
是否一致，不一致公开深链会 404。

**二、`allowed_updates` 显式补 `channel_post`。**
同步靠中间件，而 `dp.resolve_used_update_types()` 只扫 handler —— 它会漏掉
`channel_post`，Telegram 就再也不推频道帖。`resolve_update_types()` 显式并上
`("channel_post", "edited_channel_post")`，[test_sync_middleware.py](../../tests/test_sync_middleware.py)
有测试盯着。

**三、双水位。**

| 字段 | 含义 |
|---|---|
| `last_seen_message_id` | 同步看到的最大 id，**含**公告闲聊 |
| `last_stored_message_id` | 索引实际更新到哪 |

两个都要：只看 `last_stored` 的话，「一个月没发新番」和「同步彻底挂了」在
数据上长得一模一样，而这两件事的处置方式截然不同。

水位用 `MAX()` 更新，不是直接赋值 —— 编辑旧帖会带来较小的 message_id，
直接赋值会让水位倒退。

查看：`/syncstat`（管理员）或

```bash
uv run python -m animebot syncstat
```

## 补漏：只能报告，不能修复

update 会丢（停机、网络抖动、bot 被临时移出频道）。`message_id` 在频道内单调
递增，所以库里的 max 和频道当前水位之间的差就是缺口。

拿"频道当前水位"有个不需要 MTProto 的办法：往频道发一条消息再删掉，返回的
`message_id` 就是水位。代价是频道里闪一条消息。

但**缺口里的消息内容拿不到** —— 又撞回 Bot API 读不了历史这面墙。所以
`gap_report()` 只报告缺口，需要重新导出 JSON 才能补。

这个结论不好听，但它是真的。不要为了"自动化"去上 MTProto ——
风险权衡见 [ADR-0002](../ADR/0002-export-json-as-backfill-source.md)。

## 边界情况

| 情况 | 处理 |
|---|---|
| 帖子被删除 | Bot API **不推送删除事件**。库里留一条死记录，点进去 404。频道很少删帖，可接受 |
| 非帖子消息 | `parse_message` 返回 `SKIPPED`，不入库，但**记 last_seen** —— 它证明 update 还在到达 |
| 媒体组（多图） | 只有第一条带 caption，后续正文为空 → 返回 `None`，天然跳过 |
| 纯文件（无说明文字的压缩包） | 不是帖子；记一行 `channel_files`，结果 `attached` / `file`。水位只动 `last_seen`（`last_stored` 只跟帖子走） |
| 带说明文字的文件 | 既是帖子也是文件，两边都写；文件归它自己 |
| 编辑只改了图 | 正文不变，upsert 写入相同内容。无害 |
| 解析失败 | 记 `FAILED` 入库 + `warning` 日志。**不静默丢弃** |
| 同步自己抛异常 | 中间件吞掉 + `log.exception`。同步是旁路，不能拖垮主链路 |
| 频道帖触发 ErrorBoundary | `_reply()` 挡住 `ChatType.CHANNEL` —— 否则一次异常就会让 bot 在频道里公开发「内部错误，编号 xxx」 |

## 埋点与日志

```
sync.received{channel,kind=new|edited}
sync.upserted{channel,kind}
sync.file{channel,kind}       ← 频道里直接发了文件（漫画整卷）
sync.skipped{channel,reason=not_post|empty}  /  sync.skipped{reason=wrong_chat}
sync.parse_failed{channel}    ← 应始终为 0，非 0 说明频道模板变了
sync.latency{channel,kind}
```

日志事件 `sync.upserted` / `sync.file` / `sync.not_post` 都是 INFO 且带
`channel=anime|manga` —— 频道帖不进访问日志（没有 handler 接它），这几行是
「同步还在收到东西」唯一能在 `journalctl` 里看到的证据。

`sync.parse_failed` 是新格式的早期预警。频道主改了模板，这个指标会先动。

## 当前基线

```
动漫 1639 帖（2026-09-05）：total=2905  ok=1504  partial=135  failed=0
漫画  938 帖（2026-10-24）：total=1257  ok=896   partial=42   failed=0   文件 271
```

`failed=0` 是红线，[test_parser_golden.py](../../tests/test_parser_golden.py)（动漫）
和 [test_manga_parse.py](../../tests/test_manga_parse.py)（漫画）盯着。两份黄金
数据集都要绿 —— 两个频道共用一张别名表，为一边加的写法不许把另一边带坏。

## 重解析不需要重导出

`raw_text` 全量保留，改了解析器之后从库里重跑即可。**这是保留 `raw_text` 的
主要理由** —— 它让解析器可以持续演进，而不是「导出一次就定型」。
