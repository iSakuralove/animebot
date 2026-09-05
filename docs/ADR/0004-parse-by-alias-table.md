# 0004. 按行切 key 查别名表，不用子串匹配

状态: 已采纳
日期: 2026-09-05

## 背景

帖子正文是这种形状：

```
中文名: 无职英雄 ～技能什么的毫无用处～
英文名: Mushoku no Eiyuu: Betsu ni Skill Nanka Iranakatta n da ga
话数: 12
放送开始: 2025年10月1日
☺️评分：4.9 不过不失
🔐解压:blackcatunderthemoon
引索：#W #WZ
```

要把它变成结构化字段。直觉做法（旧代码就是这么写的）是拿字段名去正文里找：

```python
FIELDS_ORDER = ["中文名", "英文名", "话数", "放送开始", ...]
for field in FIELDS_ORDER:
    if field in text:
        ...
```

## 决策

**先按行切出 `key: value`，再把 key 规范化后查别名表。**

```python
kv = split_kv(line)                      # "☺️评分：4.9 不过不失" -> ("☺️评分", "4.9 不过不失")
key = normalize_key(kv[0])               # "☺️评分" -> "评分"
canonical = META_LOOKUP.get(key)         # "评分" -> "score"
```

别名表（[fields.py](../../src/animebot/domain/fields.py)）里每条写法后面标注了
它在 1639 个真实帖子中的出现次数。

## 理由

**子串匹配有必然的误匹配。** `'开始' in '放送开始: 2025年10月1日'` 是 True。
只要别名表里同时存在 `开始` 和 `放送开始`，或者未来加了任何一个是另一个子串的字段名，
就会静默错配。这不是「小概率 bug」，是数据结构选错了。

现在有测试专门盯这个：`test_substring_does_not_match_wrong_field`。

**复杂度从 O(字段数 × 正文长度) 降到 O(行数)。** 旧做法是双层循环。

**加字段只改表不改代码。** 真实数据里字段写法的长尾很长：评分前缀有
`☺️`(1266) / `⭐️`(190) / 无(21) / `个人`(6) / `👾豆瓣`(1) 五种；百度网盘有
`😱`(596) / `🗂`(515) / 无(167) / `📤`(163) / `🐢`(37) / `📥`(29) / `💘`(4) 七种。
`normalize_key()` 剥掉 emoji 装饰后全部收敛到一个 key，别名表只写素面写法。

**未知 key 进 `extra` 永不丢弃。** 这是「不破坏用户数据」的落地：帖子已经发出去了，
改不了。解析器认不出的字段留在 `extra` 里，[回归测试](../../tests/test_parser_golden.py)
会在某个未知 key 出现 ≥5 次时报红 —— 那是别名表该更新的信号，而不是数据该丢的信号。

## 代价

**标题自带冒号会被误判成字段。** 真实反例：

```
Re：从零开始的异世界生活 全系列
肌肉少女：哑铃，能举多少公斤？
Re:CREATORS
```

这三条（以及另外 2 条）曾经让 `title_cn` 完全丢失，`parse_status=FAILED`。

修法是加一条规则：**首行（`idx <= 1`）的冒号归标题**，整行收下，
打上 `title_from_first_line` 标记。5 条 FAILED 因此清零。

这个特例值得吗？值。它是**首行**这一个位置的规则，而不是散落各处的 if。
而且现在有测试 `test_titles_with_colon_survive` 和 `test_title_with_colon_kept_whole`
盯着，不会退化。

**简介正文里的冒号。** 「少年说：我要成为海贼王」在简介模式下不能被切成字段。
解法是解析器有 `in_summary` 状态：遇到「故事简介」进入，遇到已登记的字段名退出。
未登记的 key 在简介模式下直接当正文，不进 `extra`。

**别名表要人工维护。** 加新写法要有人去数一下出现次数、加一行。这是刻意的：
自动学习别名会引入错配，而错配在这种数据上是静默的。

## 相关

- [modules/parsing.md](../modules/parsing.md) —— 解析器的完整规则表
- [data-dictionary.md](../data-dictionary.md) —— 每个字段的真实覆盖率
