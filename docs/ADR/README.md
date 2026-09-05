# 架构决策记录（ADR）

一个决策一个文件。记录**当时为什么这么选**，而不是代码现在长什么样
—— 后者看代码就行，前者半年后没人记得。

## 格式

```markdown
# NNNN. 标题

状态: 已采纳 | 已废弃 | 被 NNNN 取代
日期: YYYY-MM-DD

## 背景
什么问题逼出了这个决策。有数据就贴数据。

## 决策
选了什么。一两句。

## 理由
为什么是它而不是别的。实测数据放这里。

## 代价
放弃了什么，什么时候这个决策会失效。
```

最后一节是最重要的。没有代价的决策不是决策，是废话。

## 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-metadata-only-decorator.md) | 装饰器只登记元数据，行为放中间件 | 已采纳 |
| [0002](0002-export-json-as-backfill-source.md) | 用导出 JSON 做全量回填 | 已采纳 |
| [0003](0003-no-fts5.md) | 不用 FTS5，LIKE + rapidfuzz | 已采纳 |
| [0004](0004-parse-by-alias-table.md) | 按行切 key 查别名表，不用子串匹配 | 已采纳 |
| [0005](0005-permalink-not-rerender.md) | 搜索结果给深链，不重新渲染帖子 | 已采纳 |
| [0006](0006-feature-module-protocol.md) | 模块按名字动态加载 | 已采纳 |
| [0007](0007-contextvars-for-trace.md) | 用 contextvars 传 trace 上下文 | 已采纳 |
| [0008](0008-fuzzy-on-title-only.md) | 模糊匹配只对中文名 | 已采纳 |
| [0009](0009-single-instance-lock.md) | 单实例锁 + 409 快速失败 | 已接受，待实现 |
| [0010](0010-pagination-state-in-callback-data.md) | 分页状态编进 callback_data | 已采纳 |
