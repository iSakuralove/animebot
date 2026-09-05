# 增量同步设计

**状态**：已实现（2026-09-05）
**代码**：[update_adapter.py](../../src/animebot/ingest/update_adapter.py) ·
[sync.py](../../src/animebot/ingest/sync.py) ·
[preflight.py](../../src/animebot/bot/preflight.py) ·
`ChannelSyncMiddleware`
**测试**：[test_sync.py](../../tests/test_sync.py)（25 条）·
[test_sync_middleware.py](../../tests/test_sync_middleware.py)（7 条）

契约与坑的日常参考看 [modules/ingest.md](../modules/ingest.md)。这份文档留下
**设计过程**：当时面对什么、为什么这么选、实测发现了什么。

## 问题

导出 JSON 是 2026-09-05 的快照。频道每周还在发新帖，每过一天索引就旧一点。

手动重导可行但荒谬：Telegram Desktop 导出 2905 条消息要几分钟，而增量只有
几条。更要紧的是**编辑** —— 真实数据里 1486 个帖子全部被 `edited` 过，
链接失效后作者会回去改。只索引新帖不管编辑，等于索引里存的是永远的第一版。

## 三个必须先接受的事实

**一、`edited_channel_post` 和 `channel_post` 同等重要，不是"顺便处理一下"。**

统计事实：1486/1486 的帖子有 `edited` 字段。编辑不是边缘情况，是常态。
两种 update 走完全相同的处理路径 —— upsert 幂等，不需要区分。

**二、bot 必须先加进频道当管理员，否则一条 update 都收不到。**

Bot API 的 `channel_post` 只推给频道的管理员 bot。这一步是人工的，代码里
做不了，只能在启动时检测并报错。

**三、停机期间的更新拿不回来。**

Telegram 只保留 24 小时的未取更新，而且 `runner.py` 现在传了
`resolve_used_update_types()` 但没有 `drop_pending_updates` —— 重启后会重放
积压。这对指令是坏事（用户的 `/search` 被延迟几小时执行），对
`channel_post` 是好事（补上停机期间的帖子）。

这个矛盾无法在 `start_polling` 层面区分。解法：**不依赖 update 补漏，靠定期
对账。** 见下面「补漏」一节。

## 数据流

```
channel_post / edited_channel_post
        │
        ▼ ChannelSyncMiddleware（不是 handler）
   chat.id == bot_api_chat_id ?  ──否──→ 忽略（讨论群的转发副本）
        │是
        ▼ update_to_export_shape()
   把 aiogram Message 转成导出 JSON 的形状
        │
        ▼ parse_message()          ← 与回填完全相同的解析器
        │
        ▼ upsert_many([post])      ← 与回填完全相同的写入
        │
        ▼ SQLite
```

**共用解析器和写入路径是这个设计的核心。** 两条数据入口如果有两份解析代码，
它们必然漂移 —— 回填修的 bug 增量里还在，反之亦然。

## 关键难点：两种消息形态不一样

导出 JSON 和 Bot API 的 `Message` 是两种结构：

| | 导出 JSON | Bot API Message |
|---|---|---|
| 正文 | `text` / `text_entities` | `text` 或 `caption` |
| entity | `text_entities[].href` | `entities[].url` / `caption_entities[].url` |
| 偏移单位 | 字符 | **UTF-16 码元** |
| 按钮 | `inline_bot_buttons[][]` | `reply_markup.inline_keyboard[][]` |
| 图片 | `photo`（相对路径字符串） | `photo[]`（PhotoSize 数组） |
| 时间 | `"2026-01-04T11:24:54"` | `datetime` 对象 |

**UTF-16 偏移是最容易埋雷的一条。** Bot API 的 entity offset 按 UTF-16 码元
计算，emoji（如 `💙`）占 2 个码元但 Python 里 `len()` 是 1。帖子正文里
emoji 密集（`💙故事简介`、`🔐解压`、`😱百度网盘`），直接拿 offset 当
Python 索引会错位，链接归属到错误的行。

处理方式：把文本编码成 `utf-16-le`，按字节偏移切片，再解码回来。
或者更简单 —— 用 aiogram 的 `entity.extract_from(text)`，它内部已经处理了
UTF-16 换算。**优先用后者，不要自己算。**

### 转换函数放哪

`ingest/update_adapter.py`，只做形状转换，不做语义判断：

```python
def message_to_export_shape(msg: Message) -> dict[str, Any]:
    """把 aiogram Message 转成导出 JSON 的形状，喂给同一个 parse_message。"""
```

放在 `ingest/` 而不是 `parsing/`，因为它依赖 aiogram 类型，而
[parsing](../modules/parsing.md) 那一层不该知道 aiogram 存在。

## 为什么用中间件而不是 handler

`channel_post` 不是指令，没有 `@command` 元数据。用 handler 需要注册一个
`F.chat.type == "channel"` 的过滤器，而它必须在所有指令 handler **之前**
执行，否则会被别的 router 抢先。

中间件顺序是显式的、可测的。而且频道帖同步不该经过限流和权限校验 ——
那两层是为用户指令设计的。

放在 `ObservabilityMiddleware` 内侧，这样同步耗时能被计时和埋点。

## 补漏：定期对账

update 会丢（停机、网络抖动、bot 被临时移出频道）。所以需要一个不依赖
update 的兜底。

**方案：`/resync` 管理员指令 + 启动时的缺口检测。**

`message_id` 在频道内是单调递增的。库里的 `max(message_id)` 和频道当前的
最新 id 之间如果有缺口，就是漏掉的帖子。

拿到"频道当前最新 id"有一个不需要 MTProto 的办法：**bot 往频道发一条消息
再删掉**，返回的 `message_id` 就是当前水位。代价是频道里闪一条消息。

```
库里 max(message_id) = 3948
发一条临时消息 → message_id = 3961 → 立即删除
缺口 = 3949..3960，共 12 条
```

但**缺口里的消息内容拿不到** —— 又回到 Bot API 读不了历史这个墙上。所以
对账只能**报告缺口**，不能自动修复：

```
/resync
→ 检测到 12 条缺口（3949..3960）。Bot API 读不到历史消息，
  需要重新导出 JSON 并 ingest。缺口区间已记入 ingest_runs。
```

这个结论不好听，但它是真的。不要为了"自动化"去上 MTProto —— 见
[ADR-0002](../ADR/0002-export-json-as-backfill-source.md) 里的风险权衡。

## 边界情况

| 情况 | 处理 |
|---|---|
| 帖子被删除 | Bot API **不推送删除事件**。库里会留一条死记录，点进去 404。可接受 —— 频道很少删帖，且 `/resync` 能发现 |
| 非帖子消息（公告、闲聊） | `parse_message` 返回 `SKIPPED`，不入库。逻辑与回填一致 |
| 讨论群的转发副本 | `chat.id` 校验挡掉。与回填的频道 id 校验同源 |
| 媒体组（多图帖子） | 每张图一条 `channel_post`，只有第一条带 caption。后续几条正文为空 → `parse_message` 返回 `None`，天然跳过 |
| 编辑只改了图 | 正文不变，upsert 写入相同内容。无害 |
| 解析失败 | 记 `FAILED` 入库 + 打 `warning` 日志。**不静默丢弃** —— 那会让新格式的帖子永久缺失且无人知晓 |

## 埋点

```
sync.received{kind=new|edited}
sync.upserted
sync.skipped{reason=not_post|wrong_chat|empty}
sync.parse_failed          ← 这个应该始终是 0，非 0 说明帖子格式变了
sync.latency
```

`sync.parse_failed` 是新格式的早期预警。频道主改了模板，这个指标会先动。

## 测试策略

不需要真连 Telegram：

1. **形状转换的单元测试** —— 手写一个 aiogram `Message` 对象（含 emoji 和
   `text_link` entity），断言转换后的形状与导出 JSON 一致。
2. **端到端一致性测试** —— 拿一条真实导出消息，反向构造成 aiogram Message，
   走两条路径，断言得到的 `Post` 完全相同。这个测试是防漂移的核心。
3. **UTF-16 偏移的回归测试** —— 专门用 `💙故事简介` 开头的帖子，验证链接
   归属没有错位。

## 实施后的修正

设计文档写完之后，实测推翻了两处，加了一处。

**一、`edit_date` 不是 datetime。** 设计里假设它和 `date` 一样是 datetime，
实测 aiogram 3.31 里它是 `int | None`。假设错了会在被编辑过的帖子上抛
`AttributeError` —— 而这个频道 1486/1486 都编辑过，等于每条增量都炸。
`_epoch()` 现在两种都接。

**二、时区问题设计时完全没看见。** 导出 JSON 的 `date` 是导出机器的本地时间
（+08:00），而 aiogram 给 UTC。两条入口错开 8 小时，`ORDER BY posted_at` 会
把新帖排错位置。解析器改成优先读 `date_unixtime`（覆盖率 100%），
`Post.posted_at` 统一 aware UTC。

这个 bug 只在两条入口同时存在时才显形，所以回填单独跑的时候一直是"对的"。

**三、加了双水位表。** 设计里只写了「对账靠 `/resync`」，但那是人工触发的
—— 没人会主动去查一个看起来正常的系统。`sync_state` 表在每条 update 上更新
`last_seen`（含不入库的公告）和 `last_stored`，`/syncstat` 一眼看出
「update 还在到达吗」和「索引更新到哪了」。

只记 `last_stored` 不够：一个月没发新番和同步彻底挂掉，在数据上长得一样。

## 另外发现的两个静默失败

都不在原设计里，都是实现时才想到要验证的。

**`allowed_updates` 会漏掉 `channel_post`。** 同步做成中间件，而
`dp.resolve_used_update_types()` 只扫 handler —— 它算出来的列表里没有
`channel_post`，传给 `start_polling` 之后 Telegram 就再也不推频道帖。
同步永久静默失效，日志一片安静。

实测确认：只有 message handler 时返回 `['message']`；注册了 channel_post
handler 才返回 `['channel_post', 'message']`。所以 `resolve_update_types()`
显式并上那两个类型，并且有测试盯着。

**ErrorBoundary 会在频道里公开发道歉。** `channel_post` 也是 `Message`，
`_reply()` 不挡住的话，一次同步异常就会让 bot 在 3000 人的频道里发
「内部错误，编号 a1b2c3d4」。现在遇到 `ChatType.CHANNEL` 直接抑制并记
`reply.suppressed_in_channel`。

## 遗留

**`/resync` 缺口报告没做完。** `gap_report()` 已经能算缺口，但"探频道当前
水位"那一步（发一条消息再删）还没接 —— 它需要真实频道权限才能验证，等部署
时和监控一起做。现在 `gap_report()` 不传 `live_max_id` 就只报库内水位，不会
编一个假的 gap 出来。
