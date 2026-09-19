# 默认标题搜索 + 全文回退（feat/static-title-search）

状态：设计中
分支：`feat/static-title-search`

## 要解决的问题

现在 `/s 青春` 把两种命中混在一起返回：

1. 标题里有"青春"的番（用户想要的）
2. 简介/staff 里提到"青春"的番（噪音）

用户打 `/s 青春` 的本质意图是「**有没有叫青春的番**」——这是对标题的查询。
把简介命中混进来，既不符合心智，又把结果撑多、触发翻页。

## 关键洞察：这同时是性能问题的解

一个被误解的前提先纠正：**搜索没有网络查询**，数据全在本地 SQLite，检索
10~28ms。用户感知的「慢」是 Telegram 投递消息的往返（~350ms/次），翻页每次
都要一次往返。老架构「一点即达」不是因为它"静态"，而是因为它**搜完发一条就
结束，只有一次往返**。

标题命中天然就少（实测）：

| 查询 | 全文命中 | 仅标题 |
|---|---|---|
| 青春 | 几十条 | 3~5 |
| 无职英雄 | 1 | 1 |
| 进击的巨人 | 4 | 4 |

**标题结果少 → 一条消息装得下 → 不翻页 → 一次往返 → 老架构的手感自然回来。**
所以「默认搜标题」一箭双雕：既对齐意图，又消掉翻页往返。这才是"静态架构"
的真正含义——不是预生成、不是绕开 SQLite（查询才 10ms，不是瓶颈），而是
**让默认路径的结果少到不需要翻页**。

## 两个入口，两种模式

| 入口 | 模式 | 行为 |
|---|---|---|
| 纯文本（**仅私聊**） | 标题模式 | 只搜标题（中文名+英文名+别名）；零命中走 fuzzy 纠错 |
| `/s <词>`（任意场景） | 全文模式 | 标题优先；标题零命中才回退全文，带明显提示 |

- **纯文本只在私聊触发。** 群里 catch 所有消息会把每句闲聊当搜索，是灾难。
  群里搜索必须显式 `/s`。
- **「标题」= `search_blob`** = 中文名 + 英文名 + 别名。搜 "Mushoku" 命中英文名
  算标题命中。不含简介 / staff / 标签 / 评分。
- **回退用 A 方案**：标题**零命中**才回退全文，顶部一行提示：
  `没匹配到标题，以下是"青春"相关内容：`。标题有命中时**绝不**混入全文命中。

## 实现

改动集中在检索层 + 一个新的私聊消息 handler，不推翻现有代码。

### 1. `_rank_all` 加 `title_only` 开关

现在的隐式级联（标题不足一页就自动扩全文）拆成显式两路：

```python
async def _rank_all(self, raw, *, title_only: bool) -> tuple[list[SearchHit], bool]:
    # 返回 (结果, fell_back)。fell_back=True 表示标题零命中、回退了全文。
    ...
    title_hits = [...]              # 只用 like_titles
    if title_hits:
        return sorted, False
    if title_only:
        # 标题模式：零命中就走 fuzzy 纠错（仍是标题域），不碰全文
        return fuzzy_or_empty, False
    # 全文模式：标题零命中才回退
    body_hits = [...]               # like_fulltext
    return (body_hits or fuzzy), bool(body_hits)
```

- `title_only=True`（纯文本默认）：只搜标题，零命中 fuzzy 纠错。
- `title_only=False`（`/s`）：标题优先，零命中回退全文，`fell_back` 标记出来。

### 2. `SearchPage` 带上 `fallback` 标记

presenter 靠它决定要不要打「以下是相关内容」那行提示。字段加进 dataclass，
默认 `False`，不破坏现有构造。

### 3. 私聊纯文本 handler

search 模块加一个 message handler：

```python
@router.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def on_plain_text(message, search, ...):
    # 走 title_only=True
```

- `F.chat.type == "private"`：只在私聊。
- `~F.text.startswith("/")`：不抢指令。
- 群里不注册这个 handler，所以群里纯文本无反应，符合预期。

### 4. `/s` 改成 `title_only=False`

`cmd_search` 传 `title_only=False`，渲染时若 `page.fallback` 为真，
在列表顶部加提示行。

## 兼容性（Never break userspace）

- `search()` / `check()` / CLI：默认 `title_only=False`，行为不变（`/check` 本来
  就该能靠简介找到）。
- 翻页 callback：query 里不带模式标记？→ **要带**。翻页时得知道当前是标题
  模式还是全文模式，否则翻着翻着结果集变了。callback_data 加一位模式标志。
- 现有测试：`test_search.py` 断言的多词/标签/fuzzy 行为都在全文模式下，不变。
  新增标题模式的测试。

## 待验证

- callback_data 加模式标志后仍 ≤64 字节（现在最坏 90 字节走 token，加 1 字节无碍）。
- 私聊 message handler 不会和 `/s` 等指令抢（`~startswith("/")` 应能隔开，要测）。
