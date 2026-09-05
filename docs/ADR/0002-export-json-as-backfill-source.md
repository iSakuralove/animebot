# 0002. 用 Telegram Desktop 导出 JSON 做全量回填

状态: 已采纳
日期: 2026-09-05

## 背景

频道有 1639 个历史帖子要建索引。但 **Bot API 读不到频道历史消息**：

- 没有 `getChatHistory`，没有 `searchMessages` —— 那些是 MTProto 的能力
- Bot 只能收到它加入频道**之后**的 `channel_post`

三条备选路：

| 方式 | 代价 |
|---|---|
| Telegram Desktop 导出 JSON | 手动一次，格式最完整（含 entities / inline_bot_buttons） |
| Telethon / Pyrogram 用户账号遍历 | 要手机号登录，有风控风险，可重复跑 |
| bot 监听 `channel_post` | 零成本，但只覆盖未来 |

## 决策

**导出 JSON 做全量回填 + bot 监听做增量维护。** 不上 MTProto。

导出时**不勾选任何媒体下载**（包括默认勾着的语音/视频消息）。

导入前强制校验 `type` 含 `channel` 且 `id` 与配置一致，不符就拒绝。

## 理由

**导出 JSON 的结构完整度足够。** 实测 2905 条消息里拿到了：`id`、`date`、`edited`、
`text_entities`（含 `href`）、`inline_bot_buttons`、`photo` 元信息。1500+ 个帖子
的下载链接只存在于 entity 的 `href` 里，可见文字只有「点击下载」—— 这些用 HTML
导出会全部丢失（HTML 还会分页成 `messages.html`、`messages2.html`...）。

**MTProto 的额外收益是零。** 它能提供的东西导出 JSON 都有，代价却是手机号登录 +
风控风险。这是典型的「为了理论完备性引入实际复杂度」。

**不导出媒体，因为导出的媒体没法用。** 导出格式里 photo 字段只是本地文件相对路径
（`chats/chat_01/photos/photo_2@29-10-2021_09-30-00.jpg`），不是 `file_id`。而
`file_id` 是 per-bot 凭证 —— Bot API 文档明确写 `file_unique_id` 跨 bot 一致但
「Can't be used to download or reuse the file」。所以即使搞到别的 bot 的 file_id
也不该用。搜索结果走原帖深链，图片由 Telegram 自己渲染。

不勾媒体还省了几个小时：3000 个帖子的图片有几 GB，纯文本导出几十秒。

**频道 id 校验是必须的，不是防御性编程。** 这个错真实发生过：第一次导出的是
讨论群（`动漫🍵馆`，`public_supergroup`，12.6 万条消息），里面的帖子全是频道帖的
`forwarded_from` 副本。而帖子正文自己写着「讨论中的链接是失效的」。灌进来会污染
整个索引，所以默认拒绝而不是警告，要强行导入得显式 `--force`。

## 代价

**回填是手动的。** 每次想全量校准都要人去 Telegram Desktop 点一次导出。缓解：
回填幂等（主键是 `(channel_id, message_id)`），随时可以重跑；日常靠增量同步，
全量回填只在解析器大改后才需要。

**导出文件的 schema 可能变。** Telegram 加过字段（`effect`、`invoice`、`media_ttl`），
解析器对未知字段是宽容的（进 `extra`），所以加字段不会炸。真正的风险是**改字段名**，
那时 [test_parser_golden.py](../../tests/test_parser_golden.py) 会红。

**bot 加入频道之前、导出之后的窗口期会漏帖。** 重跑一次回填即可补齐。

## 相关

- [design/incremental-sync.md](../design/incremental-sync.md) —— 增量那一半
- [modules/ingest.md](../modules/ingest.md) —— 回填模块契约
