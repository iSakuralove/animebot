# storage —— SQLite 仓储

一张主表 + 一张标签表 + 一张审计表。只依赖 `domain`。

## 为什么是 SQLite

1639 个帖子，全表扫 LIKE 0.5~3ms（实测，见 [ADR-0003](../ADR/0003-no-fts5.md)）。
上 PostgreSQL 是给自己找运维活儿干；上向量库是给不存在的问题买解决方案。

单文件、零运维、`raw_text` 全存着随时能重建 —— 这个规模下没有比它更合适的。

## 表设计的一条线

**查询维度进关联表，载荷进 JSON 列。**

| 字段 | 存法 | 理由 |
|---|---|---|
| `tags` / `index_tags` | 关联表 `post_tags` + 索引 | 要做组合筛选（`#奇幻 且 #异世界`）和标签云 |
| `aliases` / `staff` / `links` / `passwords` / `extra` | JSON 列 | 只在展示时整块读出，从不按它们筛选 |
| `search_blob` | 冗余列，存小写 | 中文名+英文名+别名拼一起，一次 LIKE 覆盖三个字段 |

`staff` 有 20 种职位且长尾（`chara_design` 只有 15 次），为每种开一列是把
数据结构问题推给 schema。JSON 一列解决。

`search_blob` 存小写的理由：SQLite 的 `LIKE` 默认只对 ASCII 大小写不敏感，
存小写 + 查询时 `.lower()` 让英文名匹配稳定，不依赖 collation。

## 没有迁移框架

DDL 全是 `CREATE TABLE IF NOT EXISTS`，启动时无脑跑一次 `init_schema()`。

真到了要改列的那天：`raw_text` 还在，**整库重解析比写 migration 快**，而且
顺带修复了历史数据里的解析错误。这是保留 `raw_text` 的第二个理由。

## upsert 不是可选项

真实数据里 **1486 个帖子有 1486 个 `edited` 字段** —— 每一条都被编辑过。
所以写入只能是 `ON CONFLICT(channel_id, message_id) DO UPDATE`，主键是
Telegram 自己的天然主键。

副作用：回填幂等，同一份导出灌 10 次结果一样。增量同步也直接复用这条路径。

`post_tags` 的更新是先 `DELETE` 再 `INSERT`，因为帖子编辑后标签可能减少 ——
只 `INSERT OR IGNORE` 会留下已删除的标签。

## `_hydrate` 与 N+1

`row_to_post()` 只还原主表字段，`tags` / `index_tags` 是空的。
`_hydrate()` 按 `channel_id` 分组后一次 `IN (...)` 查完所有标签。

**任何新增的查询方法都必须走 `_hydrate`**，否则返回的 Post 标签是空的 ——
这个错很安静，因为标签为空不会报错，只是筛选结果变少。

## 三个检索方法的分工

| 方法 | 扫什么 | 实测 | 用途 |
|---|---|---|---|
| `like_titles` | `search_blob` | 0.5ms | 主路径 |
| `like_fulltext` | `search_blob` + `raw_text` | 3ms | 标题不够时兜底 |
| `all_titles` | 只取 `title_cn` | — | 喂 rapidfuzz |

`all_titles` 故意不返回 `search_blob`：拼上英文名后长达 80+ 字符，
`WRatio` 对长串惩罚很重，实测「无值英雄」会被「英雄时代」反超。
见 [ADR-0008](../ADR/0008-fuzzy-on-title-only.md)。

## `ingest_runs`：为什么要审计表

回填是可以重跑的，所以每次跑的成功率必须留痕。没有它，「上次改解析器之后
成功率掉了」这件事只能靠人肉记忆。

```sql
SELECT started_at, total, ok, partial, failed, note FROM ingest_runs ORDER BY id DESC LIMIT 5;
```

`finish_run` 在 `finally` 里调用 —— 中途崩了也要留下记录，否则最需要审计的
那次反而没记录。

## 并发

`PRAGMA journal_mode=WAL` + `busy_timeout=5000`。

WAL 让「bot 在读」和「增量同步在写」不互相阻塞。单写者仍然是硬限制，
但 bot 只有一个进程（见 [ADR-0009](../ADR/0009-single-instance-lock.md)），
不存在多写者。
