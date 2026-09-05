# 动漫频道检索 bot

在自己频道已发布的 1639 个动漫帖子上做检索：输入名字，判断发过没有，发过就给原帖链接。

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

填 `AnimeCoffeebot=<token>` 和 `ANIMEBOT_ADMIN_IDS=<你的 user_id>`，然后回填数据：

```bash
uv run animebot ingest "C:\Users\Administrator\Downloads\Telegram Desktop\频道json数据"
```

```bash
uv run animebot run
```

## CLI

| 命令 | 用途 |
|---|---|
| `animebot ingest <路径>` | 从导出 JSON 全量回填，幂等 |
| `animebot stats` | 库状态、解析成功率、标签分布 |
| `animebot search <关键词>` | 不连 Telegram 直接搜，调试用 |
| `animebot commands` | 列出已注册指令，验证模块装配 |
| `animebot run` | 启动 bot（long polling） |

## 指令

| 指令 | 说明 |
|---|---|
| `/search <关键词>` | 搜索。多词、错别字容错、`#标签` 筛选 |
| `/check <番名>` | 只回答发过 / 没发过 |
| `/tags` | 标签列表；`/tags 引索` 看拼音首字母索引 |
| `/help [指令]` | 从注册表自动生成 |
| `/ping` | 测活，返回 trace_id |
| `/health` `/metrics` | 管理员专用 |

搜索支持：

```
/search 无职英雄          精确
/search 无职英雄 技能      多词，命中越多排越前
/search 咒术回站          打错字也能搜到
/search #奇幻 #异世界      标签交集
/search #百合 学园        标签 + 关键词
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
│   ├── middlewares.py   trace / 错误边界 / 权限 / 限流 / 埋点
│   ├── wiring.py        @command → aiogram Router
│   └── loader.py        按名字动态加载模块
├── features/            功能模块，加模块不改其它文件
│   ├── system/          /help /ping /health /metrics /trace
│   └── search/          /search /check /tags
├── domain/              Post 模型 + 字段别名表
├── parsing/             帖子文本解析
├── storage/             SQLite 仓储
├── search/              检索服务 + 展示层
└── ingest/              导出回填
```

## 设计取舍

**装饰器只登记元数据，行为全在中间件。** `@command` 不包裹函数，所有保护
（trace、埋点、限流、权限、错误边界）由中间件统一做一次。写新指令的人不可能
「忘记加某个装饰器」而少一层保护，栈追踪也不会被多层包装搞脏。

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

**`callback_data` 只放 `p:<message_id>`。** Telegram 上限 64 字节，真实数据里
的 sharepoint 链接有 200+ 字符，往里塞 URL 必爆。

## 测试

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

129 个测试，其中 [test_parser_golden.py](tests/test_parser_golden.py) 把 1639 个
真实帖子当黄金数据集：`failed` 必须永远是 0，每个字段的填充数不许下降。解析器
改坏了立刻红。

基线（2026-09-05 实测）：

```
消息 2905  ok 1504  partial 135  failed 0  skipped 1266  帖子 1639

title_cn 100.0%   links      98.4%   passwords 98.2%
tags      97.4%   summary    96.6%   index_tags 91.3%
episodes  90.3%   staff      90.4%   score      89.7%
```

## 加新模块

见 [docs/adding_a_feature.md](docs/adding_a_feature.md)。要点：建一个包、导出
`FEATURE`、往 `ANIMEBOT_FEATURES` 加名字。不改任何已有文件。
