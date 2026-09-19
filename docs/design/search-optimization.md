# 检索优化算法与存储结构调研

状态：调研备忘（条件触发的路线图，未立项）
日期：2026-09-19
前置：[ADR-0003](../ADR/0003-no-fts5.md)（现在为什么不用 FTS5）、
[ADR-0008](../ADR/0008-fuzzy-on-title-only.md)（模糊为什么只对 title_cn）、
[modules/search.md](../modules/search.md)（检索契约与失效天花板）

## 为什么有这份备忘

ADR-0003 记了「现在不用 FTS5」的实测依据，modules/search.md 写了「~5 万帖时
设计失效」。但真到那天该换成什么、候选算法各自长什么样，没查过。这份备忘把
调研结论存档，到时候直接翻出来用，不必重新查一遍。

结论先行：**检索的三个环节——子串预筛、模糊纠错、相关性排序——在 1639 行
上，现状都是调研结果里的最优档。所有「更高级」的算法，收益都要数据涨两个
数量级（~5 万帖）才兑现。**

## 子串预筛（现状：`LIKE '%kw%'` 全表扫，0.5~3ms）

前导通配符让 B-tree 索引全部失效，全表扫是结构性的。能换的只有索引结构：

| 结构 | 能解决什么 | 对本项目 |
|---|---|---|
| 倒排索引（FTS5 内部即此） | 分词 / 前缀匹配 | 不解决任意子串匹配 |
| trigram（SQLite 3.34+ 内置） | 任意子串走索引 | **两字中文查询全 miss**——ADR-0003 实测过，不可用 |
| CJK bigram 倒排 | 两字查询直接命中 | **真正到时候的方案**，见下节 |
| 后缀数组 / 后缀自动机 | 子串检索理论最优 | 工程复杂度远超收益 |

trigram 的坑值得再钉一次：索引单元是 3 字符，**查询 <3 字符退化为全表扫**。
第三方 sqlite-better-trigram 能索引 2 字符，但 FTS5 的 API 不允许自定义
tokenizer 参与 LIKE/GLOB 加速——主要收益没了。所以「到时候」也不要用内置
trigram，这条路对两字中文是死的。

## 模糊纠错（现状：全量拉 title_cn + rapidfuzz WRatio，1447 条 10.5ms）

| 算法 | 查询复杂度 | 预计算 | 对本项目 |
|---|---|---|---|
| rapidfuzz（现状） | O(n) 位并行，C++ 实现 | 无 | **就是最优解** |
| SymSpell | O(1) | 预计算 delete 邻域，内存大 | 词条 10 万+ 才有收益；且纯 Python 实现大概率输给 C 位并行 |
| BK-tree | O(log n) | 无 | 常数大，同上 |
| LinSpell | O(n) | 无 | SymSpell 作者的无预计算版本；本质等价于现状做法 |

值得做的微优化只有一个：`_fuzzy` 每次都 `all_titles()` 全量拉取再建 dict，
10.5ms 里大头大概率是这次查询和建 dict，不是 WRatio 计算（待实测）。
启动时加载一次 + upsert 时增量维护即可消掉，改动集中在 repo 和 service 两处。

## 相关性排序（现状：手写分层 `rank()`）

现状的分层打分（exact > prefix > title > alias > body > fuzzy，加位置/长度
衰减）本质是简化版 BM25F（分字段加权词频）+ 业务规则。ADR-0003 已论证这套
业务规则 BM25 表达不出来，所以现状不仅够用，而且更合适。迁倒排索引那天才
重新评估 FTS5 的 `bm25()` / `rank`。

## 「到时候」的方案：bigram 倒排表

两字中文查询的正解是 CJK bigram——Lucene 的 CJKAnalyzer、MySQL 的 ngram
parser 都是这条路。SQLite 里不需要 FTS5 自定义 tokenizer（那要 apsw 或 C
扩展），一张普通表就够：

```sql
CREATE TABLE search_grams (
    gram       TEXT    NOT NULL,   -- search_blob 的相邻两字，已 lower()
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (gram, channel_id, message_id)
) WITHOUT ROWID;
CREATE INDEX idx_grams_post ON search_grams (channel_id, message_id);  -- upsert 反删用
```

- 2 字查询：`WHERE gram = ?` 一步命中。
- n 字查询：拆成 n-1 个 bigram 求 AND 交集，和 `by_tags` 的
  `HAVING COUNT(DISTINCT tag) = ?` 同构（repo.py 已有先例）。
- 写入：upsert 路径 delete + insert，和 post_tags 完全同一模式
  （`upsert_many` 已有先例）。每帖 search_blob ~80 字符 → ~79 行 gram。

成本核算（按 1639 帖时代的数据）：LIKE 0.5ms 换 bigram <0.1ms，省 0.4ms
不值一张表 + 一致性维护。数据到 ~5 万帖、LIKE 到 100ms 量级时这笔账才反过来。

## 存储引擎路线

| 阶段 | 选择 |
|---|---|
| 现在（<1 万帖） | SQLite 单文件 + WAL，不动 |
| ~5 万帖（LIKE 到 100ms） | 方案 A：上节的 bigram 倒排表。`like_titles` / `like_fulltext` 已经把候选来源隔离成一个函数，换实现不动打分（ADR-0003 预留的迁移路径仍然成立，只是换的表不是 FTS5 虚表） |
| 检索需求复杂化、不想自研 | 方案 B：Meilisearch / Typesense。typo tolerance、CJK 分词、BM25 开箱自带，本文全篇等于买现成的；代价是多一个要运维的服务 |

## 触发阈值（什么时候翻出这份备忘）

- 帖子 > 5 万，或 LIKE 预筛实测 > 50ms → 重读「bigram 倒排表」一节
- 模糊纠错 > 50ms → 先做 `all_titles()` 缓存，不够再评估 SymSpell
- 出现「按长文本做相关性搜索」的新需求 → 直接评估方案 B

按每年 ~350 帖算，5 万帖是 100 年后的事。先到的多半是方案 B 的触发条件。

## 来源（2026-09-19 检索）

- SQLite FTS5 官方文档（trigram tokenizer、bm25）：sqlite.org/fts5.html
- sqlite-better-trigram（索引 2 字符但失去 LIKE/GLOB 加速）：github.com/streetwriters/sqlite-better-trigram
- SymSpell: 1000x Faster Spelling Correction：seekstorm.com/blog/1000x-faster-spelling-correction-algorithm
- LinSpell（SymSpell 作者的无预计算版本）：github.com/wolfgarbe/LinSpell
- BK-tree 介绍与实现：geeksforgeeks.org/bk-tree-introduction-implementation
