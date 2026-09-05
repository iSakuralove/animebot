# 文档索引

文档只记录**代码回答不了的问题**：为什么这么选、边界在哪、坑在哪。
「这个函数做什么」去看代码和它的 docstring，不要在文档里重复一遍——那是必然过期的一份。

文件名用 ASCII，内容用中文。文件名带中文会让 git、shell、CI 都多一层引号地狱。

## 读什么

| 你想知道 | 看 |
|---|---|
| 这个项目做什么、不做什么 | [PRD.md](PRD.md) |
| 整体怎么搭的、一次请求怎么走 | [architecture.md](architecture.md) |
| 某个决定为什么这么做 | [ADR/](ADR/README.md) |
| Post 的字段从哪来、覆盖率多少 | [data-dictionary.md](data-dictionary.md) |
| 某个模块的契约和坑 | [modules/](modules/) |
| 怎么加一个新指令模块 | [adding_a_feature.md](adding_a_feature.md) |
| 增量同步怎么设计的 | [design/incremental-sync.md](design/incremental-sync.md) |
| 部署和监控 | [operations.md](operations.md) |

## 目录

```
docs/
├── PRD.md                        产品需求：范围、验收、不做什么
├── architecture.md               架构总览：分层、依赖方向、请求旅程
├── data-dictionary.md            Post 字段字典 + 真实覆盖率
├── adding_a_feature.md           加新模块的操作手册
├── operations.md                 部署、监控、故障排查（待部署时补齐）
├── ADR/                          架构决策记录，一决策一文件
│   ├── README.md                 格式说明与索引
│   └── NNNN-<标题>.md
├── modules/                      分模块契约文档
│   ├── core.md                   内核：注册表、容器、错误、HTTP
│   ├── observability.md          trace、日志、埋点
│   ├── domain.md                 领域模型与字段别名表
│   ├── parsing.md                帖子解析
│   ├── storage.md                SQLite 仓储
│   ├── search.md                 检索与展示
│   ├── bot.md                    aiogram 装配层
│   └── ingest.md                 回填与增量同步
└── design/                       具体功能的设计文档
    └── incremental-sync.md
```

## 写文档的规矩

1. **有实测数据就贴数字。** 「LIKE 够快」是废话，「1639 行 0.5ms」是论据。
2. **决策进 ADR，不进代码注释。** 注释解释这段代码，ADR 解释为什么不是另一种代码。
3. **过期的文档比没文档更坏。** 与代码耦合紧的细节（函数签名、字段列表）留在代码里；
   文档只写不随重构改变的东西：契约、边界、理由。
4. **不写"未来可能"。** 待办进 [PRD.md](PRD.md) 的路线图，一处即可。
