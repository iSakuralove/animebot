# parsing —— 帖子解析

把一条 Telegram 消息（导出 JSON 或实时 update）变成 [`Post`](domain.md)。

只依赖 `domain`。不碰数据库、不碰 aiogram、不发网络请求 —— 所以 129 个测试里
解析相关的部分全是纯函数测试，不需要任何夹具。

## 两个文件的分工

| 文件 | 负责 |
|---|---|
| [text.py](../../src/animebot/parsing/text.py) | 文本层：拍平 entity、切 key/value、抓 hashtag |
| [post_parser.py](../../src/animebot/parsing/post_parser.py) | 语义层：查别名表、判断字段归属、定 parse_status |

分开的理由：文本层是纯机械操作，跟"动漫帖子"这个业务无关，将来解析别的
频道格式可以直接复用。

## 边界：这一层不知道的事

- **不知道频道 id 从哪来。** 由调用方传入。解析器不做「这是不是我的频道」的判断，
  那是 [ingest](ingest.md) 的职责。
- **不做去重、不做入库。** 返回 `Post` 对象就结束，写不写库不关它的事。
- **不校验语义合法性。** `话数: 十二` 会原样存进 `episodes`，不会报错。
  帖子已经发出去了，改不了，解析器的职责是尽量读懂而不是判卷。
- **不 import aiogram。** 输入永远是一个 dict（导出 JSON 的形状）。增量同步那边
  由 [`update_adapter`](../../src/animebot/ingest/update_adapter.py) 先把
  `Message` 转成这个形状。这条边界让解析器能被 1639 个帖子的黄金数据集单独
  测试，不需要造任何 aiogram 对象。

## 时间：只读 `date_unixtime`

`_parse_dt()` 优先读 `<key>_unixtime`（导出里覆盖率 100%），退回 `date` 字符串。

因为 `date` 是**导出机器的本地时间** —— 这个频道的导出全是 +08:00。拿它当 UTC
会让整批数据偏移 8 小时，而增量同步那边 aiogram 给的是真 UTC，两条入口就此
错开，`ORDER BY posted_at` 把新帖排错位置。

输出一律 aware UTC。

## 坑一：`text` 字段有两种形态

导出 JSON 里，无 entity 的消息 `text` 是字符串，有 entity 的是
`[str | {type, text, href}]` 混排数组。直接当字符串用会 `TypeError`。

`flatten()` 统一处理，优先读 `text_entities`（那里连 plain 段也被包成对象，
结构一致），退回 `text`。

## 坑二：链接只存在于 entity 里

1500+ 个帖子的下载链接，可见文字是「点击下载」「打开」，甚至是单个 `\n`。
URL 只在 entity 的 `href` 字段里。

所以不能靠正则从正文里抓 URL —— 正文里根本没有。必须记录每个 entity 在拍平
文本中的**字符偏移**，再用 `bisect` 换算成行号，才能把 URL 归属到
「🗂百度网盘：点击下载」这一行。

```
行 12: 🗂百度网盘：点击下载
                    ↑ 偏移 384，落在第 12 行 → links["baidu"] = href
```

这是整个解析器里唯一有点技巧的地方，也是唯一无法靠猜写对的地方。

## 坑三：首行的冒号不是字段分隔符

```
Re：从零开始的异世界生活
肌肉少女：哑铃，能举多少公斤？
```

老帖子标题不带 `中文名:` 前缀，而标题自己带冒号。规则：**第 0~1 行、
key 查不到别名表、长度 2~200 → 整行当标题收下**，并记 `title_from_first_line`
到 `parse_notes` 以便审计。

行号限制是必要的：不限制的话正文里任何一个未知 key 都会覆盖标题。

## 坑四：简介正文里也有冒号

```
💙故事简介
以日本著名文学家坂口安吾所作《明治开化安吾捕物帖》为蓝本：将这部时代剧...
```

所以简介模式下遇到未登记的 key **不退出简介模式**，整行并入简介。
只有命中别名表的真字段才终止简介 —— 简介永远在字段区之前结束，这是
1486 个帖子共同的结构。

## parse_status 的判定

`_STRONG = {title_cn, episodes, index, tags, air_date}` —— 这五个字段的
出现率都在 90% 以上，是"这是一个番剧帖"的强信号。

| 条件 | 状态 | 含义 |
|---|---|---|
| 无标题、无强信号 | `SKIPPED` | 公告、闲聊，不是帖子 |
| 无标题、有强信号 | `FAILED` | 是帖子但标题没解出来 —— **必须永远是 0** |
| 有标题、≥2 强信号 | `OK` | |
| 有标题、1 强信号或有简介 | `PARTIAL` | 参与检索，但字段少得可疑 |

`FAILED` 是回归测试的红线。[test_parser_golden.py](../../tests/test_parser_golden.py)
拿真实导出跑，`failed != 0` 直接失败。

## 改这一层的规矩

**加新写法只改 [fields.py](../../src/animebot/domain/fields.py) 的别名表，不改 post_parser.py。**

别名表里每条写法后面标了真实出现次数。加新写法时把次数一起写上 —— 那是
将来判断"这个别名还有没有用"的唯一依据。

改了解析逻辑之后，跑：

```bash
uv run python -m animebot ingest "C:\path\to\export" --dry-run --stats
```

对比 `ok/partial/failed` 和字段填充数。任何一项下降就是回归。
`raw_text` 全量保留，所以整库重解析不需要重新导出。
