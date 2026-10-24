# 动漫 / 漫画频道检索 bot

在自己频道已发布的帖子上做检索：输入名字，判断发过没有，发过就给原帖链接。
两个频道：动漫频道（1639+ 帖，私聊直接发字 / `/s`）和漫画频道（ACG Library，
938 帖，`/c`）。

数据来源是 Telegram Desktop 的导出 JSON —— **Bot API 读不到频道历史消息**
（没有 `getChatHistory` / `searchMessages`，那是 MTProto 的能力），所以历史帖子
只能导出一次灌进来，之后靠 `channel_post` / `edited_channel_post` 增量维护。

## 快速开始

```bash
uv sync
```

```bash
cp .env.example .env
```

填 `AnimeCoffeebot=<token>` 和 `ANIMEBOT_ADMIN_IDS=<你的 user_id>`，然后回填数据
（哪个频道由导出文件自己的 id 决定，两个频道各灌一次）：

```bash
uv run animebot ingest "C:\Users\Administrator\Downloads\Telegram Desktop\频道json数据"
uv run animebot ingest "C:\Users\Administrator\Downloads\Telegram Desktop\ChatExport_2026-10-24 (1)"
```

```bash
uv run animebot run
```

## CLI

| 命令 | 用途 |
|---|---|
| `animebot ingest <路径>` | 从导出 JSON 全量回填，幂等；按导出里的 id 认是哪个频道 |
| `animebot stats` | 每个频道的帖子 / 文件数、解析成功率、标签分布、同步水位 |
| `animebot syncstat` | 增量同步诊断：update 还在到达吗、索引更新到哪 |
| `animebot search <关键词> [--channel manga]` | 不连 Telegram 直接搜，调试用 |
| `animebot commands` | 列出已注册指令，验证模块装配 |
| `animebot run` | 启动 bot（long polling） |

## 指令

| 指令 | 说明 |
|---|---|
| 私聊直接发字 | 搜动漫。标题命中优先，打错字会纠错 |
| `/s <关键词>` | 搜动漫，严谨模式：标题精确匹配，不猜错字 |
| `/c <关键词>` | **搜漫画频道**。中文名 / 别名 / 日文原名、`#标签`、打错字给「名字相近」；不带词给热门标签按钮 |
| `/check <番名>` | 只回答发过 / 没发过 |
| `/tags` | 标签列表；`/tags 引索` 看拼音首字母索引 |
| `/help [指令]` | 从注册表自动生成 |
| `/health` `/metrics` `/syncstat` `/ping` `/trace` | 管理员专用（`ANIMEBOT_ADMIN_IDS`） |

搜索支持：

```
/s 无职英雄          精确
/s 无职英雄 技能      多词，命中越多排越前
咒术回站             （私聊直接发）打错字也能搜到
/s #奇幻 #异世界      标签交集
/c 进击的巨人         漫画；结果行带作者 · 卷数 · 连载年份 · 📦频道内整卷文件
/c #百合 #校园        漫画标签交集
```

动漫结果页上有 **`📚 漫画`** 按钮（翻页行上方），点击用同一个词切到漫画结果，
漫画结果页上有 `🎬 动漫` 切回来 —— 空结果页也有，正好用来「这边没有去那边找」。

结果多于一页时下面出现翻页行 `⏮ ◀ 4/72 ▶ ⏭`。序号是全局连续的（第 2 页显示
9~16），关键词在标题里高亮。CLI 用 `--page` 翻页：

```bash
uv run animebot search "#漫改" -n 5 --page 3
```

## 结构

```
src/animebot/
├── config.py            集中配置（pydantic-settings）
├── cli.py               命令行入口
├── core/                内核，不依赖 aiogram
│   ├── registry.py      @command 装饰器 + 指令注册表
│   ├── container.py     依赖容器
│   ├── feature.py       模块协议
│   ├── errors.py        错误分类（UserError vs 我们的 bug）
│   └── http.py          外部 API 基类（超时/重试/缓存/埋点）
├── observability/
│   ├── context.py       contextvars 贯穿的 RequestContext
│   ├── logging.py       structlog，控制台给人看 / 文件 JSON 给机器看
│   └── metrics.py       进程内计数器 + 直方图
├── bot/
│   ├── app.py           装配：中间件顺序、模块加载、关停
│   ├── middlewares.py   trace / 错误边界 / 权限 / 限流 / 埋点 / 频道同步
│   ├── wiring.py        @command → aiogram Router
│   ├── loader.py        按名字动态加载模块
│   ├── preflight.py     启动自检：bot 在每个频道里是管理员吗
│   └── ui.py            翻页就地编辑（两个搜索模块共用）
├── features/            功能模块，加模块不改其它文件
│   ├── system/          /help /ping /health /metrics /syncstat /trace
│   ├── search/          动漫：私聊纯文本 /s /check /tags
│   └── manga/           漫画：/c（落地页标签按钮、结果翻页）
├── domain/              Post / ChannelFile 模型 + 字段别名表 + 链接分类
├── parsing/             帖子 / 文件消息解析（不依赖 aiogram）
├── storage/             SQLite 仓储
├── search/              检索服务 + 展示层 + 分页编解码 + 高亮
└── ingest/              导出回填 + 增量同步 + Message→导出形状转换
```

## 设计取舍

**装饰器只登记元数据，行为全在中间件。** `@command` 不包裹函数，所有保护
（trace、埋点、限流、权限、错误边界）由中间件统一做一次。写新指令的人不可能
「忘记加某个装饰器」而少一层保护，栈追踪也不会被多层包装搞脏。

**两条数据入口共用同一个解析器。** 增量同步先把 aiogram `Message` 转成导出
JSON 的形状，再喂给和回填完全相同的 `parse_message`。两份解析代码必然漂移
—— 回填修的 bug 增量里还在，而黄金回归测试只覆盖回填那条路。

**不用 FTS5。** 实测 trigram tokenizer 对中文 2 字查询全部未命中（要求至少
3 字符），而 LIKE 全表扫 1639 行只要 0.5~3ms、rapidfuzz 模糊排序 10ms。
在这个数据量上架倒排索引是纯粹的过度设计，还要维护影子列同步。

**模糊匹配只对 `title_cn`。** 拼上英文名和别名后长达 80+ 字符，WRatio 对长串
惩罚重，`无值英雄` 会被 `英雄时代` 反超。scorer 对比过 5 种，只有 WRatio
在全部测例上把正确答案排第一。

**`raw_text` 和 `parse_status` 是强制字段。** 1639 个帖子跨四年半发出，格式
漂移是常态（评分前缀有 `☺️`/`⭐️`/无/`个人` 四种，百度网盘有 7 种 emoji 变体）。
解析器要适应已发布的数据，而不是要求回去改 3000 个帖子。原文保留后，解析器
改版可以整库重跑，不必重新导出。

**时间一律 aware UTC。** 导出 JSON 的 `date` 是导出机器的本地时间（+08:00），
而 aiogram 给真 UTC。混用会让两批数据在 `ORDER BY posted_at` 里错开 8 小时。
解析器优先读 `date_unixtime`。

**`callback_data` 只放 `p:<message_id>` 或 `s:<page>:<query>`。** 上限是 64
**字节**不是字符，一个汉字 3 字节。查询词塞得下就内联（无状态、重启不失效），
超长退到短 token + 进程内 LRU。真实数据里的 sharepoint 链接有 200+ 字符，
往里塞 URL 必爆，而报错发生在用户点击时而非发送时，很难复现。
见 [ADR-0010](docs/ADR/0010-pagination-state-in-callback-data.md)。

**翻页不缓存结果集。** 一次完整检索实测 12~35ms，而缓存要处理失效、内存增长、
「翻页时帖子被编辑了」的一致性问题。排序是全序，所以重查顺序一致，翻页不会
重复或漏项。

**高亮先在原文里定位，再逐段转义。** 顺序反了两种都错：先转义就得在 `&amp;`
里找关键词，先插标签就会把 `<b>` 自己转义掉。另外不能用 `text.lower()` 的偏移
切原文 —— `"İ".lower()` 是 2 个字符，会错位。

## 静默失败的防线

这类故障下进程活着、指令能用、日志干净，只有某个功能悄悄不工作 —— 比崩溃更
难发现，所以每一条都有主动检测：

| 故障 | 防线 |
|---|---|
| bot 不是频道管理员 → 收不到 `channel_post` | 启动自检 + `/health` |
| `allowed_updates` 漏了 `channel_post` | `resolve_update_types()` 显式补，有测试 |
| 同步停了没人发现 | `sync_state` 双水位 + `/syncstat` |
| 频道模板变了 | `sync.parse_failed` 埋点应始终为 0 |
| 两条入口解析结果漂移 | `test_roundtrip_matches_export` |

已知缺口：两个进程同用一个 token 时会各拿一半更新（409），而
`TelegramConflictError` 从 polling 循环抛出、`ErrorBoundary` 抓不到。
待部署时补单实例锁，见 [docs/ADR/0009](docs/ADR/0009-single-instance-lock.md)。

## 测试

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

395 个测试。三个是回归红线：

- [test_parser_golden.py](tests/test_parser_golden.py) —— 1639 个真实动漫帖子当
  黄金数据集，`failed` 必须永远是 0，每个字段的填充数不许下降。
- [test_manga_parse.py](tests/test_manga_parse.py) —— 938 个真实漫画帖子同理，
  外加 271 个文件全部找得到主人。两个频道共用一张别名表，两份都要绿。
- [test_sync.py](tests/test_sync.py) 的 `test_roundtrip_matches_export`（漫画在
  `test_manga_parse.py`）—— 真实消息反向构造成 aiogram Message，逐字段断言两条入口
  结果相同。

基线：

```
动漫（2026-09-05）消息 2905  ok 1504  partial 135  failed 0  帖子 1639
漫画（2026-10-24）消息 1257  ok 896   partial 42   failed 0  帖子 938  文件 271
```

## 加新模块

见 [docs/adding_a_feature.md](docs/adding_a_feature.md)。要点：建一个包、导出
`FEATURE`、往 `ANIMEBOT_FEATURES` 加名字。不改任何已有文件。

## 文档

| 想知道 | 看 |
|---|---|
| 做什么、不做什么、验收基线 | [docs/PRD.md](docs/PRD.md) |
| 分层、依赖方向、一次请求的旅程 | [docs/architecture.md](docs/architecture.md) |
| 某个决定为什么这么做 | [docs/ADR/](docs/ADR/README.md) |
| 某层的契约和坑 | [docs/modules/](docs/modules/README.md) |
| 部署、监控、排查 | [docs/operations.md](docs/operations.md) |
