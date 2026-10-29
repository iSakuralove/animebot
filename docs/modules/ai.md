# ai —— `/q` AI 对话

五个文件，一个职责一句话：

| 文件 | 负责 |
|---|---|
| [__init__.py](../../src/animebot/features/ai/__init__.py) | 模块装配：建 client / toolbox / engine / 会话表，放进容器 |
| [client.py](../../src/animebot/features/ai/client.py) | Anthropic Messages 协议的 SSE 客户端（只做协议，不懂工具） |
| [tools.py](../../src/animebot/features/ai/tools.py) | 五个工具：Bangumi 三个 + Bing 搜索 + 频道索引 |
| [chat.py](../../src/animebot/features/ai/chat.py) | 工具循环、多轮上下文、主/兜底模型降级 |
| [stream.py](../../src/animebot/features/ai/stream.py) | 流式落地：草稿流 / 原地编辑 + 回放节奏 |
| [render.py](../../src/animebot/features/ai/render.py) | Markdown → Telegram HTML |
| [session.py](../../src/animebot/features/ai/session.py) | `(chat_id, user_id)` 维度的会话表 |
| [handlers.py](../../src/animebot/features/ai/handlers.py) | `/q` 指令：选流式方式、停止、分片发送 |

`client` / `chat` / `stream` 互不 import 上层，可以脱离 Telegram 单独测
（`test_ai.py` 里 90% 的用例不造 `Message`）。

## 一次 `/q` 的调用链

```
cmd_q(message, engine, conversations, settings)
  │  /q 重置 → conversations.reset()
  │  选 public = chat_type in ("group","supergroup")
  ▼
_generation(gid) + _make_streamer(...)
  │  私聊 & stream_mode=draft → DraftStreamer（草稿，客户端做动画）
  │  其它                      → EditStreamer（占位消息 + 原地 edit）
  │  两者都起 pump() 协程，节流往外推
  ▼
ChatEngine.answer(conversation, question, on_text, on_notice, cancel)
  │  循环 1..max_tool_rounds+1：
  │    ClaudeClient.stream_round(messages, tools)
  │      逐帧 SSE → text_delta 累积成「到目前为止的全文」→ on_text
  │                thinking_delta → on_thinking（按时间报进度）
  │                input_json_delta → 拼 tool_use.input
  │    有 tool_use → ToolBox.run() → tool_result 回灌 → 下一轮
  │    没有         → 收尾，返回最后一轮文本
  ▼
_deliver(message, streamer, text, public)
  │  草稿：settle() 等落地 → sendMessage 正式消息
  │  编辑：finish() 把最终文本 edit 进占位消息
  ▼
Markdown → HTML → 超 3500 字分片 → ui.reply
```

## 契约与不变量

**引擎不认识 Telegram，handler 不认识 SSE。** `ChatEngine.answer` 的输入是
`Conversation` + 问题字符串，输出是 `ChatResult`；中间的 `on_text` 是同步回调
（不能 await，否则拖慢读流）。

**`on_text` 收到的是「到目前为止的完整文本」，不是增量。** 上层直接拿它渲染
一版，不做拼接。这条不写清楚就会出现双重累积。

**工具失败不抛异常。** `ToolBox.run` 把异常转成 `{"error": "..."}` 的 JSON 给
模型看 —— 模型会自己换策略，一次工具挂了不该让整轮对话崩掉。

**thinking 块不进 history。** `stream_round` 返回时已经剥掉；
`_strip_foreign_keys` 再兜一次（只留 text / tool_use / tool_result 和
`tool_use_id`、`input` 这些协议字段）。留一个 `cache_control` 上去，网关就 400。

**会话键是 `(chat_id, user_id)`。** 群里两个人同时问不能看到对方上下文；
私聊里两者相等，一套键覆盖两种场景。

## 五个工具

| 工具 | 端点 | 用它的场景 |
|---|---|---|
| `bangumi_search` | `POST /v0/search/subjects` | 有片名：拿 id、评分、排名 |
| `bangumi_detail` | `GET /v0/subjects/{id}` | 「这部讲什么」：简介、话数、制作、评分分布 |
| `bangumi_browse` | 同一搜索接口的 filter | **没有片名**：「今年穿越番」「近十年最高分」 |
| `web_search` | Bing HTML | 时效信息；网关会丢服务端工具，只能自己搜 |
| `channel_search` | 本地 SQLite（复用 `SearchService`） | 「频道里发过没」 |

Bangumi 结果走进程内 TTL 缓存（600 秒），`detail` 和 `browse` 共用同一个
`_cache`。缓存键是请求体的 JSON 序列化，所以同一个关键词不会重复出网。

## 坑（都踩过）

**1. 网关的 thinking 关不掉，也回不去。**
`thinking: {type: disabled}` 直接 400；把返回的 thinking 块原样回传也 400。
所以每轮请求前必须剥掉，而模型每轮都会产出它。

**2. 服务端工具被静默丢弃。**
请求里放 `web_search_20250305` 不报错、不联网，模型会回答「我没有联网能力」。
联网搜索只能客户端实现再回灌。

**3. `sort=score` 会把 1 人打 10 分的条目排第一。**
「近十年评分最高」的正确做法是配 `rating_count` 下限。`bangumi_browse` 在
`sort=score|heat` 且调用方没给 `min_votes` 时**强制**加 `>= 300`，系统提示里
让模型自己给 1000。

**4. `sort=rank` 会把未排名条目（rank=0）排最前。**
给 `max_rank` 时强制加 `rank >= 1`。

**5. `limit` 服务端硬顶 20。** 传 100 也只回 20，不做客户端翻页。

**6. 中文关键词必须 UTF-8 JSON body。**
用 `curl -d` 传中文（Windows 按 GBK 发出去）得到 `total: 0` —— 看起来像
「搜不到」，其实是编码坏了。

**7. `sendMessageDraft` 只支持私聊。** 群里发直接
`TEXTDRAFT_PEER_INVALID`。所以群里退回「占位消息 + 原地 edit」，没有动画。

**8. 草稿和正式消息会并存 —— 收尾要等一个往返。**
文档说「bot 发消息时草稿消失」，实测只在草稿请求全部落地之后成立。所以
`DraftStreamer.settle()` 先 drain 完、再等 0.5 秒、然后才发正式消息。
漏了这步的表现是「bot 回了两条一样的」。

**9. `Message.reply` 会继承入站的 ephemeral。**
群里从 `/` 菜单选的指令本身是 ephemeral 消息（菜单标了 `is_ephemeral`），
`reply` 会自动把回复也变成 ephemeral。**公开回复必须走 `Message.answer`** ——
`bot/ui.py` 的 `reply(public=True)` 就是这么实现的。

**10. 4096 是「解析后的字符数」，不是字节。**
实测：4096 字 OK，4097 字 `MESSAGE_TOO_LONG`；1050 个 `&` 渲染成
`&amp;`（原始 5250 字节）仍然 OK。1024 只约束图片/视频的 caption。
`split_text` 按可见字符切 3500，渲染后永远不会超。

**11. 网关的正文是「突发」的。**
实测思考 1~3 秒、工具 0.3~4 秒、正文 0.7 秒出 130 个 delta。按真实到达节奏
刷，那 0.7 秒里一版都刷不出来 —— 观感就是「没有流式」。所以有回放层（见下）。

## 流式：两条路 + 回放

| 场景 | 方式 | 代价 |
|---|---|---|
| 私聊 | `DraftStreamer` | 客户端做动画、最顺滑；结束必须补一条正式消息 |
| 群聊 / `stream_mode=edit` | `EditStreamer` | 只有一条消息；编辑是硬替换，看得出跳 |

两条路共用 `_Pacer`：回放 + 节流 + 光标。

**回放按时间摊开，不按比例。** 整段文字在 `max(0.8s, 字数/280)` 秒内均匀长完
（上限 2.5s）。按「每帧推剩余量的 35%」会前几帧猛推、后面几乎不动，看起来是
「跳一下再卡住」—— 这是一开始被反馈「僵硬」的原因。

**正文不足 12 字时不露出来**，继续显示「正在思考…」。模型一个 delta 一个字地
吐，露出来就是一帧「近▌」再猛跳到几十字。

**光标 `▌` 隔帧切换**，只在正文已经上屏时才画。

**节流间隔 0.25~0.3 秒。** 实测 `editMessageText` RTT 中位 355ms、
`sendMessageDraft` 0.25s 稳 / 0.20s 起撞 429。所以真实上限是 1.5~2 版/秒，
想更密只能换离 Telegram 更近的机器。并发流水线试过：2 路 3.6 版/秒但 30%
429 且可能乱序（文本回缩），不值得。

## 指令可见性

`/q` 在群菜单里**不标** `ephemeral`（`@command(group_ephemeral=False)`）——
答案是公开的，问题藏着就变成「群里只看到 bot 在自说自话」。`/s` `/c` `/help`
保持默认（查询词是私事）。

停止按钮也分两套：草稿用 Telegram 自带的（`can_stop=True`，回传
`stopped_message_generation` 带 `draft_id`），编辑用自己画的（callback
`qs:<gid>`）。公开回答时按钮全群可见，`_Generation.requester_id` 挡住旁观者。

## 什么时候这套设计会失效

- **网关改协议**（强制 OpenAI 形状）：`client.py` 整个重写，其余不动。
- **Bing 改版**：`parse_bing` 返回空，模型答「没查到」。换引擎只改 `web_search`。
- **上下文要跨重启**：现在是进程内存。把 `ConversationStore` 换成 SQLite 实现，
  接口不变。
- **Telegram 给草稿加群支持**：`_make_streamer` 里的判断去掉一行即可。
