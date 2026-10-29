# 群聊检索（@bot / 回复 / ephemeral）

## 用户看到什么

群（例如 `https://t.me/asxascsaxsax`）里有三种用法：

```
点输入框的 / 选 /s          → 严谨搜索（命令消息别人看不见）
/s@AnimeCoffeebot 无职英雄   → 严谨搜索（等同于上一条）
（回复 bot 任意一条消息）无职英雄 → 宽松搜索
@AnimeCoffeebot                  → 列出能用的指令（只有自己看得到）
@AnimeCoffeebot 无职英雄         → 宽松搜索（见下，投递不保证）
/c@AnimeCoffeebot 进击的巨人     → 搜漫画频道
```

从群里的 `/` 菜单选择指令时，**用户发的那条命令本身也只对该用户可见**
（`BotCommand.is_ephemeral`，菜单用 `BotCommandScopeAllGroupChats` 单独推）。
所以查询词不会留在群聊记录里。

**但这个标记是逐条可控的**（`@command(group_ephemeral=False)`）。检索类要保持
私密（查询词是私事），而 `/q` 必须关掉 —— 它的答案已经公开了，问题再藏起来
就变成「群里只看到 bot 在自说自话」。

搜索结果是 ephemeral 消息：只有发起人看得到，带可点的标题深链和翻页按钮。
翻页走 `editEphemeralMessageText`，不会给群里发任何东西。

## 三条入口的投递保证不一样

Telegram 文档列出的「隐私模式下也能收到」只有：

- 明确发给 bot 的指令（`/cmd@bot`）；
- 对 bot 消息的回复；
- 通过 bot 发出的 inline 结果。

**普通 @提及不在名单上。** 实测里 bot 是群管理员时能收到（管理员本来就收全部
消息），不是管理员时不保证。所以：

| 入口 | 投递 |
|---|---|
| 菜单里的 `/s`（ephemeral） | 保证（它是指令） |
| `/s@bot` | 保证 |
| 回复 bot 的消息 | 保证 |
| `@bot 关键词` | 不保证；bot 是管理员时可靠 |

功能说明里把菜单 `/s` 放在第一位，就是这个原因 —— 它是唯一同时满足「保证
送达」和「不打扰别人」的入口。@提及仍然保留：管理员 bot 场景下它最顺手，
而且收到也不会出错。

## 为什么不注册群里的纯文本

私聊里「直接发文字 = 搜索」很顺手，群里照搬会把每句闲聊当搜索 ——
[ADR-0012](../ADR/0012-title-first-search.md) 的结论。而且 Telegram 的隐私模式
默认就不投递群里的普通消息，这个 handler 在群里本来就是死代码。

群聊只接「用户主动指向 bot」的消息：指令、回复、@提及。

## 私密回复的前提：bot 是群管理员

Telegram 的 ephemeral 消息（Bot API 10.2）有两种发送方式：

1. **有触发动作**：收到回调按钮或 ephemeral 指令后 15 秒内，带上
   `callback_query_id` / `reply_parameters.ephemeral_message_id`。
2. **bot 是群管理员**：随时可发给任意非 bot 成员，不需要触发动作。

`@bot 关键词` 和「回复 bot」都不属于第 1 类（它们不是 ephemeral 指令，
也没有 callback），所以**非管理员的 bot 发不出私密回复**，只能公开回复。
代码里的处理是降级而不是报错：

```
ui.reply()  群里普通消息
   ├─ ephemeral 发送成功  → 只有发起人看到
   └─ 失败（非管理员/老客户端）→ 退回普通 reply，功能可用但不私密
```

所以部署清单里「把 bot 设为群管理员」不是可选优化：它同时决定
（a）能不能私密回复、（b）群里的所有消息是否都能收到。

## 代码落点

| 位置 | 职责 |
|---|---|
| [bot/ui.py](../../src/animebot/bot/ui.py) | `reply()` 决定公开还是 ephemeral；`safe_edit()` 按消息类型选编辑 API |
| [bot/help.py](../../src/animebot/bot/help.py) | `render_command_list()`，`/help` 和 `@bot` 共用 |
| [features/search/handlers.py](../../src/animebot/features/search/handlers.py) | `_MentionsMe` / `_RepliesToMe` 两个过滤器 + `on_mention` / `on_reply_to_me` |
| [bot/app.py](../../src/animebot/bot/app.py) | `sync_bot_commands()` 追加 `BotCommandScopeAllGroupChats`，指令标 `is_ephemeral` |

`reply()` 不接收 `bot` 参数：aiogram 把 `bot` 注入 update 的 context，
`Message.bot` 一定拿得到；多传一个参数只会让每个 handler 多写一遍
`bot=bot`（这个中间版本写过，删掉了）。

## 降级矩阵

| 场景 | 收到消息 | 回复可见性 |
|---|---|---|
| 私聊 | 是 | 只有用户 |
| 菜单里选 `/s`（ephemeral） | 是 | 只有用户 |
| 群里 `/s@bot` | 是 | bot 是管理员 → ephemeral；否则公开 |
| 回复 bot 的消息 | 是 | 同上 |
| 群里 `@bot ...` | 不保证（管理员 bot 可靠） | 同上 |
| 群里普通闲聊 | 否（隐私模式） | — |

最后两行是设计目标，不是缺陷。想让 bot 看见全部群消息（例如做日志分析），
只能加管理员或关隐私 —— 那会把闲聊全送进检索链路，不值得。

## 测试

[test_group_ephemeral.py](../../tests/test_group_ephemeral.py) 钉住四件事：

1. `@bot 关键词` 与「回复 bot」都触发搜索，且用 ephemeral 参数发送；
2. 群里闲聊/@别的 bot 不触发；
3. ephemeral 发送失败时退回公开回复；
4. ephemeral 消息翻页用 `EditEphemeralMessageText`，普通消息仍然用
   `EditMessageText`。

## 上线前要做的两件事

1. **把 bot 拉进群**（否则连 `/s@bot` 都收不到）。
2. **设为群管理员**（否则私密回复不可用，结果会公开在群里）。
   日志里持续出现 `reply.ephemeral_failed` 就是这个信号。
