# AI 对话（`/q`）

## 用户看到什么

```
/q 进击的巨人在 bangumi 上评分多少？
   ↓ 草稿区先出现「🔍 正在查 Bangumi…」
   ↓ 然后逐字变成「8.2 分，排名 #114…」
   ↓ 生成结束，草稿消失，正式消息落进聊天记录
```

群里的 `/q` **公开回答**：AI 的答案是全群都有用的信息，藏起来没道理。
（搜索结果相反 —— 那是某个人在找东西，公开等于替他刷屏，见
[group-search.md](group-search.md)。）

群里发不了草稿（`TEXTDRAFT_PEER_INVALID`），所以走「占位消息 + 原地编辑」，
可见性由 `ui.reply(public=True)` 决定。停止按钮全群可见，但只有提问者能按 ——
别人点了会收到「只有提问的人能停止」。

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

## 流式：两种落地方式，按场景选

| 场景 | 方式 | 为什么 |
|---|---|---|
| 私聊 | `sendMessageDraft` | 客户端把同一 `draft_id` 的更新做成**动画**，1.5 版/秒也顺滑 |
| 群聊 | 占位消息 + `editMessageText` | 草稿不支持群；编辑换来「只有一条消息」和 ephemeral |

两条路共用同一层回放（见下），只有回放比例不同：草稿 0.22（有动画，慢一点
更像打字），编辑 0.35（没动画，太慢会显得卡住）。

`ANIMEBOT_AI_STREAM_MODE=draft|edit` 可以全局切成编辑模式（想少一条正式
消息时用），群聊不受它影响。

### 草稿流的两个坑

**一、结束必须补一条正式消息。** 草稿是临时的（30 秒到期、`message_id` 恒为 0），
不补的话用户过一会儿什么都看不到。

**二、补之前要等一个往返。** 第一次上线时用户看到「回了两条一样的」——
根因不是草稿本身，是**收尾太急**：文档说「bot 发消息时草稿消失」，但那只在
草稿请求全部落地之后才成立。现在 `settle()` 先推完手里最后的内容（否则泵
还没跑过，草稿一版都没出现），再等 0.5 秒，然后才发正式消息。

### 编辑模式的代价

`editMessageText` 一次约 340ms 往返（VPS 到 Telegram），最多 ~3 版/秒，
而且是硬替换没有过渡 —— 看得出是一跳一跳的。这是用户反馈「没有草稿那种
丝滑感」的原因。它保住的只有两点：群里能用、只有一条消息。

### 两条路的节流都是 RTT 决定的

实测（VPS 直连）：

```
editMessageText RTT   中位 355ms（最小 332ms）
sendMessageDraft      0.25s 间隔稳，0.20s 开始 429
```

所以间隔设 0.25~0.3s，真实帧率上限就是 ~1.5~3 版/秒。想更密只能少一次往返，
没有别的办法。

### 为什么还要「回放」

一手实测（2026-10-29，deepseek-v4.1-flash）：

| 阶段 | 耗时 | 产出 |
|---|---|---|
| 思考（强制 thinking，关不掉） | 1~3s | 零个正文 token |
| 工具调用 | 0.3~4s | 零个正文 token |
| 正文 | **0.7s 出 130 个 delta** | 一整段，一次性涌进来 |

正文只有一个「突发」。按真实到达节奏刷的话，那 0.7 秒里最多出 1~2 版就直接
终态 —— 观感就是「没有流式」。

所以 `on_text` 收到完整正文进缓冲区，显示进度按**时间**往前推：整段文字在
`max(0.8s, 字数/280)` 秒内均匀长完（上限 2.5s）。为什么是时间而不是「每帧推
剩余量的百分之几」——后者前几帧猛推一大段、后面几乎不动，看起来是「跳一下
再卡住」，第一版就是这么做的，被反馈「僵硬」。

另外三条观感规则（都是实测踩出来的）：

- **正文不足 12 字时不露出来**，继续显示「正在思考…」。模型一个 delta 一个字
  地吐，露出来就是一帧「近▌」再猛跳到几十字。
- **光标 `▌` 隔帧切换**。Telegram 没有原生打字光标，自己画一个才有「还在写」
  的感觉。只在正文已经上屏时才画 —— 挂在「正在思考…」后面像打错字。
- **草稿收尾不跳到全文**。它后面紧跟着正式消息，不需要完整；强制跳会让最后
  一眼是「啪一下变完整」。编辑模式相反，那一条就是最终产物，必须完整。

这不造假：回放的全是已经拿到的真实内容；`finish()` / `settle()` 时
`_stopping` 置位，`_advance` 直接到头，不会因为「还没播完」而截断。

### 停止按钮也是两套

- 草稿：`can_stop=True` 让 Telegram 自带一个「停止生成」，按下回传
  `stopped_message_generation`（只带 `draft_id`），`_BY_DRAFT` 表找回取消信号。
- 编辑：自己画的 `⏹ 停止` 按钮，callback_data 是 `qs:<短id>`。

### 出错时不留两条

模型/网关报错时先清掉手里那条（草稿等落地，占位消息删除），再把异常抛给
`ErrorBoundary` —— 它本来就会发一条带 trace_id 的说明。

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

### 长度上限：4096 字符，不是 1024

新手最容易记错的一条。实测（2026-10-29）：

| 内容 | 结果 |
|---|---|
| 正文 4096 字 | OK |
| 正文 4097 字 | `MESSAGE_TOO_LONG` |
| 1050 个 `&`（渲染后 5250 字节） | OK —— 算的是**解析后**的字符数 |
| 编辑到 4096 / 4097 字 | 同上 |

1024 只约束**图片/视频的 caption**，纯文本消息是 4096。所以 `split_text`
按可见字符切在 3500（留余量给 HTML 实体和光标），渲染后永远不会超。

`&` 撑成 `&amp;` 这种「原始变长」不影响 —— Telegram 数是解析后的字符。测试
里钉了 `test_rendered_length_never_exceeds_raw_length` 和
`test_streaming_never_sends_over_limit`。

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
