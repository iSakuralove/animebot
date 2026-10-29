# 0015. 群聊检索：@bot / 回复 bot 触发，结果走 ephemeral

状态: 已采纳
日期: 2026-10-28

## 背景

频道成员不只在私聊里搜。讨论群（`https://t.me/asxascsaxsax`）里有人想直接
`/s 无职英雄`，有人想 `@bot` 问一下有什么指令，然后接着发关键词。

群聊有两个和私聊完全不同的约束：

1. **bot 默认看不到群里的普通消息。** 隐私模式是默认值，只保证投递「明确
   针对 bot」的消息：`/cmd@bot`、回复 bot 的消息、通过 bot 发的 inline 结果。
   普通 @提及**不在文档保证的名单上**（bot 是管理员时实测能收到，因为管理员
   本来就收全部消息）。把纯文本 handler 装到群里「让每句话都能搜」既收不到，
   真收到了也是灾难（[ADR-0012](0012-title-first-search.md)）。
2. **搜索结果不能刷屏。** 一条结果消息带 8 条标题 + 网盘链接，在群里发等于
   给所有人推送一次别人的搜索。Bot API 10.2 起有 ephemeral messages：只对
   指定用户可见，正好解决这个。

## 决策

**三个入口，一套检索：**

| 入口 | 触发 | 投递保证 | 回复 |
|---|---|---|---|
| 菜单里选 `/s`（`is_ephemeral`） | 指令 | 有 | ephemeral |
| `/s@bot 关键词`、`/c@bot 关键词` | 指令 | 有 | ephemeral |
| 回复 bot 的任意消息 + 关键词 | 回复 | 有 | ephemeral |
| `@bot 关键词` | @提及 | 不保证（管理员 bot 可靠） | ephemeral |

`@bot` 不带关键词 = 指令清单（和 `/help` 同一份渲染，`bot/help.py`）。

**回复的私密性由 `bot/ui.reply()` 统一决定，handler 不写分支：**

- 私聊 → 普通 `reply`。
- 入站本身就是 ephemeral → `Message.reply` 自动带 ephemeral 参数。
- 群里普通消息 → 显式 `EphemeralMessageParameters(receiver_user_id=发送者)`；
  失败（非管理员 / 老客户端 / 超过 15 秒）**退回公开回复**。

**群菜单标 `is_ephemeral=True`**（`BotCommandScopeAllGroupChats`）。用户从
菜单选指令时，那条消息对群里其他人不可见 —— 不然「@bot 问一句、结果私密」
还是会把查询词留在群里。

**逐条可控**：`@command(group_ephemeral=False)` 不标。检索类保持默认（查询词
是私事），`/q` 关掉 —— 它的答案是公开的，问题藏着就变成 bot 自说自话。

## 理由

- **不注册群里的纯文本 handler。** 隐私模式本来就收不到；关掉隐私去收，等于
  把每句闲聊都送进检索，还白烧一次限流配额。@提及和回复已经覆盖「用户主动
  找 bot」这个意图。
- **ephemeral 是平台能力，不是我们的开关。** 它要求「有触发动作」或「bot 是
  群管理员」。所以代码不能假设私密一定成功 —— 失败降级成公开回复，用户至少
  拿得到结果，而不是发完 @bot 没反应。
- **指令清单和 `/help` 共用 `render_command_list`。** 两份列表必然漂移，
  而「菜单里说支持、真发说不认识」是最难查的一类不一致。
- **聊天里不留翻页状态。** ephemeral 消息也带 inline 键盘，翻页走
  `editEphemeralMessageText`（message_id 恒为 0，`editMessageText` 用不了）。
  `safe_edit` 按 `ephemeral_message_id` 分流，两个搜索模块的翻页代码一行没改。

## 代价

- **群必须把 bot 加进去**，否则什么都收不到。要私密回复还得是管理员 ——
  非管理员只能在 15 秒内、且拿着触发动作的 id 才能发 ephemeral，而 @提及
  不提供这个 id。文档里把「加为管理员」写成部署前提。
- **不是管理员时退化成公开回复**，一条搜索结果会出现在群里。这是有意的：
  可用性优先于隐私。想要隐私就设管理员。
- **ephemeral 消息不保证送达**（用户离线时尤其），也不会进聊天记录 ——
  用户翻回去找不到上次的搜索结果。这是平台语义，不是 bug。
- **依赖 Bot API 10.2+**（aiogram 3.31 已支持）。老客户端看不到 ephemeral
  消息，会表现为「bot 没反应」。
