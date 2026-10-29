# AI 对话（`/q`）

## 用户看到什么

```
/q 进击的巨人在 bangumi 上评分多少？
   ↓ 草稿区先出现「🔍 正在查 Bangumi…」
   ↓ 然后逐字变成「8.2 分，排名 #114…」
   ↓ 生成结束，草稿消失，正式消息落进聊天记录
```

群里的 `/q` 不流式：结果走 ephemeral（只有提问者看得到），一次发完。

`/q 重置` 开新对话 —— 上下文按 `(chat_id, user_id)` 存，闲置 30 分钟自动
清空，最多保留 16 条。

## 为什么是这套协议

网关（happycoding.xyz）只支持 Claude 协议。三条实测约束决定了实现形状：

| 现象 | 后果 |
|---|---|
| 模型强制 thinking，`disabled` 会 400 | 不能关；思考 token 也计入 `max_tokens` |
| thinking 块回流给下一轮会 400 | 每轮请求前必须剥掉（`_strip_foreign_keys`） |
| 服务端工具（`web_search_20250305`）被静默丢弃 | 联网搜索必须自己实现 |

## 一条请求的旅程

```
/q 问题
  │
  ▼ handlers.cmd_q
  │  取 (chat_id, user_id) 的会话；启动草稿泵（pump 协程）
  ▼ chat.ChatEngine.answer
  │
  ├─ 循环 1..max_tool_rounds+1
  │    ├─ client.stream_round(messages, tools)
  │    │    POST /v1/messages stream=true
  │    │    逐帧解析 SSE：text_delta → on_text（草稿）
  │    │                     thinking_delta → 丢弃
  │    │                     input_json_delta → 拼成 tool_use.input
  │    ├─ 有 tool_use → tools.ToolBox.run(...) → tool_result 回灌 → 下一轮
  │    └─ 没有 tool_use → 收尾返回
  │
  ▼ 最后一轮：tools=None（逼模型用已有信息回答，不无限查）
  ▼ handlers._send_answer
       Markdown → Telegram HTML → 分片发送
       HTML 解析失败 → 纯文本重发一次
```

**草稿与最终消息的分工**：草稿里包含所有轮次说过的话（工具轮的「我查一下」
也在，否则文字会突然消失），最终消息**只取最后一轮** —— 过程话术不该进正式
回答。`ChatEngine.answer` 用 `round_start` 下标切分这两者。

## 五个工具

| 工具 | 调用时机 | 实测 |
|---|---|---|
| `bangumi_search` | 有片名：评分 / 排名 / `id` | `api.bgm.tv` 500ms；必须 UTF-8 JSON + User-Agent |
| `bangumi_detail` | 「这部讲什么」：简介、制作、话数 | `/v0/subjects/{id}`，字段全但噪音多，只回灌白名单 |
| `bangumi_browse` | 没有片名：「2026 穿越番」「近十年最高分」 | 同一搜索接口的 filter 语法 |
| `web_search` | 新番、新闻、Bangumi 查不到的 | Bing HTML；DDG 在 VPS 上返回验证页 |
| `channel_search` | 「频道里发过没」「有没有资源」 | 本地 `SearchService.search()`，5 条/频道 |

工具失败返回 `{"error": "..."}` 而不是抛异常：模型看到错误会自己换策略，
一次工具挂了不该让整轮对话崩掉。

### 为什么 `bangumi_browse` 是单独一个工具

`bangumi_search` 只能按片名匹配。用户问「2026 年有什么穿越番推荐」时没有片名，
拿 search 硬套得到的是垃圾。Bangumi 的搜索接口本身支持 filter
（`tag` / `air_date` / `rating` / `rating_count` / `rank`），把它暴露给模型就行 ——
不需要我们再写一层「推荐系统」。

三个实测坑，直接写进了工具描述和代码里：

```
limit 硬顶 20            传 100 也只回 20，不做客户端翻页
sort=score 没人数下限      "1 人打 10 分" 的冷门条目会排第一
sort=rank 有 rank=0       未排名条目排在最前 → 给 max_rank 时加 rank>=1
```

第 2 条最要命：不设 `rating_count` 时，「近十年评分最高」的答案会是
《寻灵大冒险》这种 1 人评分的条目。所以 `bangumi_browse` 在 `sort=score|heat`
且调用方没给 `min_votes` 时**强制**加 `rating_count >= 300`，系统提示里也让
模型自己给 1000。

### Bangumi 的编码坑

中文关键词必须用 UTF-8 编码的 JSON body 提交。用 `curl -d '{"keyword":"进击的巨人"}'`
（Windows 下按 GBK 发出去）会得到 `total: 0` —— 看起来像「搜不到」，其实是编码坏了。

## 流式：占位消息 + 原地 edit

**先说结论：不要用 `sendMessageDraft`。** 第一版就是用它做的，上线当天用户
截图反馈「回了两条一样的消息」—— 草稿和最终 `sendMessage` 在客户端上**同时
可见**，要等草稿自己 30 秒到期才消失。它本来就是「临时预览」语义，
`message_id` 恒为 0，渲染出来的东西不是真正落库的消息。

现在的做法是最朴素的那种：

```
1. 发一条占位消息「🤔 正在思考…」（带「⏹ 停止」按钮）
2. 生成过程中按 1.4 秒节流 edit 这条消息
3. 生成结束，把最终文本 edit 进去，摘掉按钮
4. 超长（>3500 字符）时，第一片进占位消息，其余各发一条
```

一条消息从头长到尾，聊天记录里始终只有一条。私聊和群里的 ephemeral 都成立
（`bot/ui.edit_text` 按 `ephemeral_message_id` 走 `editMessageText` 或
`editEphemeralMessageText`）。

### 为什么还要「回放」

一手实测（2026-10-29，deepseek-v4.1-flash）：

| 阶段 | 耗时 | 产出 |
|---|---|---|
| 思考（强制 thinking，关不掉） | 1~3s | 零个正文 token |
| 工具调用 | 0.3~4s | 零个正文 token |
| 正文 | **0.7s 出 130 个 delta** | 一整段，一次性涌进来 |

正文只有一个「突发」，不是稳定细流。直接把最新文本刷上去，用户在 0.7 秒里
看不到任何中间态 —— 观感就是「没有流式」。

所以 `EditStreamer` 多了一层回放：`on_text` 收到的是完整正文（进缓冲区），
每次 tick 只把**显示进度**往前推一段（剩余量的 35%，最少 24 字）。
470 字的回答在本地代理下刷 7 版、在 VPS 直连下大约 8~10 版，肉眼能看出在长。

这不造假：回放的全是已经拿到的真实内容；`finish()` 仍然一次写完整文本，
`_stopping` 置位后 `_advance` 直接到头，不会因为「还没播完」而截断。

### 节流与 429

实测：同一条消息连刷 0.2s 一版 10/10 全过、不报 429 —— 编辑不像 `sendMessage`
那样每次都产生推送，限流宽得多。所以 `interval` 取 0.25s。

`EditStreamer` 不在 SSE 解析循环里 await（那会拖慢读流），而是：

- `on_text` 只记最新文本 + 置 dirty（同步，微秒级）
- `pump()` 协程每 `interval` 秒刷一版（按「上一次发出」起算，含网络往返）
- 回放没追上缓冲区时立刻排下一版，不等新的 delta
- 撞 `TelegramRetryAfter` 就把间隔乘 1.6（上限 6 秒）并重试这一版

### 停止按钮

按钮的 `callback_data` 是 `qs:<短 id>`，`_GENERATIONS` 把 id 映射到
`asyncio.Event`。点下去 → 置位 → `client.stream_round` 的读流循环检查到就
抛 `Stopped` → handler 把当前半截文本留在消息里、前面加一句「已停止生成」。

### 出错时不能留两条

模型/网关报错时，`cmd_q` 会**先删掉占位消息**再把异常抛给 `ErrorBoundary`
（它本来就会发一条带 trace_id 的说明）。留着占位就是又一次「两条消息」。

### 为什么不用 push 式流

`editMessageText` 每调一次就是一个完整往返（VPS 直连约 150ms）。按 token 刷
（一秒几十次）会立刻撞 429，用户看到的是卡顿而不是流畅。1.4 秒的节奏是
「看得出在长」和「不撞限流」之间的折中 —— 实测一次带工具的回答 3~8 秒，
总共刷 3~6 版。

## 渲染：为什么自己写 Markdown → HTML

不用 `parse_mode=MarkdownV2`：它要求转义 18 个字符，模型漏一个整条消息 400；
而且不支持标题、代码块语言标记会原样显示。HTML 只要求 `& < >`。

`render.to_telegram_html` 的处理顺序本身是设计：

1. 先摘出代码块和行内代码（内容整体转义，不参与后续任何替换）
2. 转义正文
3. 才插标签（`**粗体**`、`_斜体_`、`[文字](url)`、`# 标题`、`- 列表`）

顺序反了两种都错：先转义再插标签会把 `<b>` 自己转义；先插标签再转义会把
`&amp;` 里的 `**` 找出来。

**表格会被压平。** Telegram 不渲染 markdown 表格，原样发出去就是一堆竖线。
系统提示里要求模型别写表格，但模型不一定听 —— `flatten_tables` 兜一层，把
`| a | b |` 变成「· a | b」。

**链接只保留 `http(s)` / `tg`**。模型可能编出 `javascript:`，放进 `<a href>`
就是可点击的坑 —— 不安全的协议退成「文字（url）」纯文本。

最后一层保险：`_send_answer` 遇到 `can't parse entities` 时**退回纯文本重发**。
用户宁可看到没格式的答案，也不能什么都收不到。

## 会话与限流

- 键 `(chat_id, user_id)`，进程内 LRU（512 个会话），闲置 30 分钟清空。
- 截断保留最近 16 条，且必须从「纯文本 user 消息」起头 —— Claude 协议要求
  user/assistant 交替且以 user 起，从 `tool_result` 处切会 400。
- `@command(rate=(4, 60))`：每分钟 4 次。一次带工具的回答要 4~8 次 API 调用，
  比搜索贵得多。

## 模型选择

主 `deepseek-v4.1-flash`，兜底 `glm-5.3-flash`。**只在第一个 token 之前降级**：
已经开始吐字再换模型，用户会看到两段文风不同的回答拼在一起。

## 配置

```bash
ANIMEBOT_AI_BASE_URL=https://happycoding.xyz
ANIMEBOT_AI_API_KEY=sk-...
ANIMEBOT_AI_MODEL=deepseek-v4.1-flash
ANIMEBOT_AI_FALLBACK_MODEL=glm-5.3-flash
ANIMEBOT_AI_MAX_TOKENS=2000       # thinking 也吃这个额度
ANIMEBOT_AI_MAX_TOOL_ROUNDS=4
ANIMEBOT_AI_TIMEOUT=120
ANIMEBOT_AI_SESSION_TTL=1800
ANIMEBOT_AI_SESSION_MESSAGES=16
```

没配 key 时模块照样加载，`/q` 回「AI 还没配好」，不影响搜索。启动日志有
`ai.not_configured` / `ai.ready` 两条，一眼能看出状态。

## 什么时候这个设计会失效

- **网关改协议**（比如强制 OpenAI 格式）：`client.py` 整个重写，其余不动。
- **Bing 改版**：`parse_bing` 返回空，模型答「没查到」。换搜索引擎只需改
  `web_search` 一个方法。
- **上下文要跨重启**：现在是进程内存。要持久化时把 `ConversationStore` 换成
  SQLite 实现，接口不变。
