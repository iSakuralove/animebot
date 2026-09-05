# domain —— 领域模型

`src/animebot/domain/`

## 职责

数据的形状，以及「真实数据长什么样」这份知识。**不 import 任何上层**
—— 谁都可以依赖它，它谁都不依赖。

| 文件 | 职责 |
|---|---|
| `post.py` | `Post` 模型 + `ParseStatus` |
| `fields.py` | 字段别名表 + `normalize_key()` |

字段的完整清单和覆盖率见 [data-dictionary.md](../data-dictionary.md)，
这里只讲契约和边界。

## post —— 帖子模型

### 主键就是 Telegram 的主键

`(channel_id, message_id)`。不另造自增 id。

后果是好的：upsert 天然幂等，回填和增量同步可以随便重跑，
`callback_data` 里只放 `message_id` 就够定位（见
[ADR-0005](../ADR/0005-permalink-not-rerender.md)）。

`channel_id` 存**不带 `-100` 前缀**的形式（跟导出 JSON 一致）。
Bot API 需要前缀时用 `Settings.bot_api_chat_id`。这个转换只在 config 里出现一次。

### 两个强制字段

```python
raw_text: str = ""
parse_status: ParseStatus = ParseStatus.OK
```

不是可选的诊断信息。理由：1639 个帖子跨四年半发出，格式漂移是常态。
原文保留后，解析器改版可以整库重跑而不必重新导出。丢了原文就等于把这个能力扔了。

### 派生属性

| 属性 | 说明 |
|---|---|
| `key` | `(channel_id, message_id)` |
| `search_title` | 中文名 + 英文名 + 别名拼一起，供 LIKE 预筛 |
| `permalink(username)` | `username` 为 `None` 时退回 `t.me/c/<cid>/<id>` |

`permalink()` 接收 `username` 参数而不是读全局配置 —— 保持 domain 层对 config
零依赖。调用方传 `settings.link_username`，那个属性会根据 `link_mode`
返回用户名或 `None`。

### ParseStatus 的语义

| 状态 | 含义 | 数量 |
|---|---|---|
| `OK` | 有标题 + ≥2 个强信号 | 1504 |
| `PARTIAL` | 有标题 + 1 个强信号或简介 | 135 |
| `FAILED` | **有标题特征却没抽出标题** | 0（必须永远是 0） |
| `SKIPPED` | 不是帖子（公告、闲聊、service） | 1266 |

`FAILED` 是「解析器有 bug」的信号，不是「数据有问题」的信号。所以
[回归测试](../../tests/test_parser_golden.py)断言它是 0。

检索只认 `OK` 和 `PARTIAL`（`repo._ACTIVE`）。

## fields —— 字段别名表

### 这是数据，不是配置

表里每条写法后面标注了**它在 1639 个真实帖子中的出现次数**：

```python
"score": ("评分", "个人评分", "豆瓣评分", "黑猫简评"),   # 1483 + 6
"baidu": ("百度网盘", "百度", "百度下载", "百度秒传",
          "秒传", "秒传见表格"),                        # 1511 + 122
```

这些数字是从真实数据统计出来的，不是设计出来的。它们的作用：

- 判断优先级（哪个是主流写法）
- 判断能不能删（出现 1 次的可能是笔误，但删了就丢数据）
- 给未来的人一个「这不是我瞎编的」的证据

### normalize_key —— 剥掉 emoji 装饰

```python
normalize_key("☺️评分")        # "评分"
normalize_key("🩶GoogleDrive")  # "GoogleDrive"
normalize_key("⛓️解压")        # "解压"
normalize_key("アニメーション制作")  # 原样保留
```

实现是一个正则：`[^0-9A-Za-z一-鿿぀-ヿー]+` 全部删掉。
保留范围包含中日文汉字、平假名、片假名、长音符 —— 因为部分帖子直接抄了日文资料
（`製作`、`音楽`、`アニメーション制作`）。

剥掉装饰后，评分的 5 种前缀变体和百度网盘的 7 种 emoji 变体全部收敛到一个 key。
所以别名表只写素面写法，加新 emoji 不需要动表。

### 三张表 + 三个常量

| 名字 | 用途 |
|---|---|
| `META_ALIASES` | 元信息字段（标题、话数、评分…） |
| `STAFF_ALIASES` | 制作人员，全部进 `Post.staff` dict |
| `LINK_ALIASES` | 资源链接，全部进 `Post.links` dict |
| `BUTTON_ALIASES` | inline 按钮文字 → 链接类型 |
| `NOISE_KEYS` | 行首词但不是字段（`https`、`PS`、`链接`…），命中即跳过 |
| `SHARED_SHEET_IDS` | 全频道公用的表格 id，解析时剔除 |

启动时 `_flatten()` 把「canonical → 别名元组」翻转成「规范化别名 → canonical」
的扁平 dict，查表 O(1)。

### 为什么 staff 是 dict 而不是列

职位有 20 多种，长尾极长：`director` 1406 次，而 `publisher` 只有 4 次、
`voice` 4 次。为每个职位开一列的话，表会有 20 个几乎全空的列，
而且加一个职位要改 DDL。

判断标准是：**这个字段会被用来查询吗？** 会 → 开列或建关联表；
只是展示 → 进 dict/JSON。目前没有「按导演搜番」的需求
（`raw_text` 的 LIKE 已经能覆盖）。

同理 `links` 是 dict，`tags` / `index_tags` 是关联表 —— 后两者要支持交集筛选。

## 加新字段的流程

1. 先统计：这个写法在真实数据里出现几次？（用 `animebot stats` 或临时脚本）
2. 往对应的 `*_ALIASES` 加一行，**注释里写次数**
3. 如果是新的 canonical 名，在 `Post` 上加字段 + 在 `_apply_meta()` 加一个 case
4. 重跑回填：`uv run animebot ingest <路径>`（幂等）
5. 跑测试。如果某个字段的覆盖率**下降**了，说明新别名抢走了别的字段的行

第 5 步是回归测试的意义：加东西不许让已有的东西变坏。

## 测试

- [test_parser_unit.py](../../tests/test_parser_unit.py) 的 `TestNormalizeKey`
- [test_parser_golden.py](../../tests/test_parser_golden.py) 的
  `test_no_unknown_frequent_keys` —— 出现 ≥5 次的未知 key 会让它红
