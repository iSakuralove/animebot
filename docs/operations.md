# 运维

**状态**：尚未部署。这份文档目前记的是**已知会遇到什么**，不是已验证的操作手册。
部署时逐条验证并把这句话删掉。

不写"未来可能"的空想 —— 下面每一条都对应一个具体的、已经查证过的失败模式。

## 启动

```bash
uv run python -m animebot run
```

启动会按顺序做这些事，任何一步的问题都在日志里有对应事件名：

| 事件 | 含义 |
|---|---|
| `feature.loaded` | 每个模块加载成功 |
| `app.wired` | 模块和指令装配完成 |
| `manga.ready` | 漫画模块就绪，报告漫画频道的帖子 / 文件数 |
| `manga.empty` | **漫画频道还没回填**，`/c` 什么都搜不到 |
| `app.ready` | 数据库连上，按频道报告帖子数和文件数 `posts={anime:…, manga:…}` |
| `ai.ready` | `/q` 可用，报告主模型 / 兜底模型 / 工具列表 |
| `ai.not_configured` | **`ANIMEBOT_AI_API_KEY` 为空**，`/q` 会回「还没配好」 |
| `bot.commands_synced` | 指令菜单已推给 Telegram |
| `bot.start` | 拿到 `getMe`，token 有效 |
| `bot.proxy` | 走了代理（`ANIMEBOT_PROXY` 非空时）|
| `preflight.channel_ok` | bot 在该频道（`channel=anime/manga`）里是管理员；`username=` 是频道真实的 username |
| `preflight.channel_unreachable` | **该频道的增量同步不会工作** |
| `preflight.username_mismatch` | 深链会指向错误的频道 |

启动阶段的 `getMe`/`setMyCommands`/`preflight` 三次 API 调用顺带把到 Telegram 的
连接预热好了 —— 用户第一次翻页不用再吃一次冷启动建连（实测 ~2000ms）。

## 漫画频道（`/c`）上线清单

数据库文件不进仓库，VPS 上的库要在 VPS 上灌。回填幂等、按频道隔开，往已有的
动漫库里灌漫画不会动动漫的数据 —— 但灌之前照样先备份。

1. **拉代码、装依赖**：`git pull && uv sync`。启动时 `init_schema()` 会给老库自动
   加上 `channel_files` 表（`CREATE TABLE IF NOT EXISTS`，不需要迁移）。
2. **备份库**：用 SQLite 的在线备份，而不是 `cp`（bot 可能正开着 WAL）：
   `uv run python -c "import sqlite3; s=sqlite3.connect('data/animebot.db'); d=sqlite3.connect('data/animebot.db.bak'); s.backup(d)"`
3. **把漫画频道的导出传上去并回填**：导出文件放在 `data/` 下（gitignored），
   `uv run animebot ingest data/manga_export/result.json`。输出应是
   `漫画频道 1659887654 … failed 0 … 写库 938 文件 271`。
4. **启用模块**：`.env` 里 `ANIMEBOT_FEATURES=system,search,manga`（显式写了这一行
   的话默认值不生效），重启。
5. **看启动日志**：`manga.ready posts=938 files=271`，`bot.commands_synced count=6`
   （多了 `/c`）。
6. **想要增量同步**：把 bot 加进漫画频道并设为管理员，否则启动日志里会有
   `preflight.channel_unreachable channel=manga`。`preflight.channel_ok` 里的
   `username=` 就是频道 username，`link_mode=public` 时抄进
   `ANIMEBOT_MANGA_CHANNEL_USERNAME`。

## 远程管理管理员名单（本机脚本）

在自己电脑上（Windows 用 Git Bash）跑 `scripts/vps-admins.sh`，直接改 VPS `.env`
里的 `ANIMEBOT_ADMIN_IDS` 并重启生效：

```bash
ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh list
ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh add 123456
ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh del 123456
```

- VPS 地址从 `ANIMEBOT_VPS` 环境变量读（不进公开仓库），部署目录可用
  `ANIMEBOT_VPS_DIR` 覆盖（默认 `/root/animebot`）。实际地址见 `CLAUDE.local.md`。
- `list` 只读；`add`/`del` 改完 `.env` 自动 `systemctl restart animebot`。
- 幂等提示：重复 add、删不存在的 id 都只回一句话，不动文件。
- **拒绝删到最后一个** —— 全删了没人能进后台。要换人：先 add 新的再 del 旧的。
- 拿别人的 user_id：让对方给 bot 随便发条消息，`journalctl` 里 `user_id=` 就有。
- 前提是本机到 VPS 免密 SSH；systemd 单元没配任何 `ANIMEBOT_*` 环境变量
  （已核对），`.env` 是唯一真相 —— 若哪天加了 `Environment=ANIMEBOT_ADMIN_IDS=...`，
  环境变量会盖过 `.env`，这个脚本的改法就失效了。

## 看日志

线上验证只有 `journalctl` 一个窗口，所以日志按「一次交互一行」设计
（[ADR-0014](ADR/0014-access-log-one-line-per-request.md)）：

```bash
journalctl -u animebot -f                                  # 实时
journalctl -u animebot --since today | grep handler.done   # 今天每一次交互
journalctl -u animebot --since today | grep scope=manga    # 只看漫画
journalctl -u animebot --since "7 days ago" | grep handler.done | grep result_count=0
                                                           # 搜了没搜到 = 该补的资源
journalctl -u animebot -p warning --since today            # 只看 WARNING 以上
```

`handler.done` 一行里有：指令、模块、结果（ok / user_error / error）、耗时、用户、
原始参数，以及搜索类指令 bind 的 `scope` / `query` / `mode` / `result_count` /
`page` / `reason`。超过 `slow_command_ms` 的同一行变成 WARNING `handler.slow`。

polling 的瞬时网络故障（`Failed to fetch updates - TelegramNetworkError …`、
`Bad Gateway`）是 WARNING，紧跟着一条 `Connection established` 就是自己恢复了。
**ERROR 级别现在只留给真问题**：`handler.crash`（带完整栈）、409 冲突、token 失效。

## 部署前必须做的三件事

### 一、把 bot 加进频道并设为管理员

Bot API 的 `channel_post` **只推给频道的管理员 bot**。没做这一步，增量同步
一条 update 都收不到 —— 而这个失败完全静默：进程活着、指令能用、日志干净。

启动自检会警告，`/health` 也会显示频道身份。但没人会主动去看一个看起来正常
的系统，所以这一步要在部署清单里。

### 二、把 bot 加进讨论群并设为管理员（群聊检索要用）

群里三种入口（`@bot 关键词`、回复 bot、`/s@bot`）的**投递**由 Telegram 的
隐私模式保证，不加管理员也能收到。但**私密回复**只能在两种情况下发：
收到触发动作后 15 秒内，或者 bot 是群管理员。`@bot 关键词` 属于前者之外的情况，
所以非管理员的 bot 只能公开回复搜索结果。

不加管理员的表现：功能可用，只是每条结果都公开在群里（日志有
`reply.ephemeral_failed`）。要私密就必须设为管理员。细节见
[design/group-search.md](design/group-search.md)。

### 三、revoke 旧 token

开发期间 `.env` 和 `venv/.env` 里各有一个 token，其中一个还在 BotFather 那边
有效。用 `/revoke` 作废不用的那个。

顺带：**开发和生产用不同的 token。** 这不是洁癖 —— 见下面 409 那一节。

## 已知缺口：409 静默半坏

**这是当前最需要补的一个洞。**

两个进程用同一个 token 同时 `getUpdates`，Telegram 只允许一条长轮询连接。
第二个连上来就把第一个踢掉返回 409；第一个重连，又把第二个踢掉。结果是两个
实例各自随机拿到大约一半的更新。

这时候的症状：

- 进程活着 → 心跳照发
- `getMe` 正常 → token 有效
- HTTP/TCP 层正常 → 任何探针都是绿的
- **但你的 bot 一半的消息不回**

比崩溃更糟。崩溃是响亮的，这个是安静的。

更要命的：**`ErrorBoundary` 抓不到它。** `TelegramConflictError` 从 polling
循环抛出，不经过 handler 链（aiogram issue #1115 里维护者明确说了）。

最容易中的场景：本地调试时开着，服务器上也在跑。

**解法不是加探针，是让它不可能发生**：启动时抢一个单实例锁，抢不到就打印
「已经有一个实例在跑，PID xxx」然后退出；捕获 409 后立即非零退出，**绝不重试**。
重试意味着接受"两个实例互相踢"这个状态，而那正是要消灭的东西。死掉是正确行为。

设计见 [ADR-0009](ADR/0009-single-instance-lock.md)。

## 监控：三层，按性价比排序

### 第一层：心跳（dead man's switch）

进程定期 GET 一个 URL，停了就告警。适合 long polling bot —— 它没有 HTTP 端口
可以被探测。

**healthchecks.io 和 Uptime Kuma push 的协议都是「GET 一个 URL」**，
Uptime Kuma 多支持 `?status=down&msg=...&ping=...` 参数。所以一个
`HeartbeatSender` 通吃两家，配置只是一个 URL，不需要为它们各写一个适配器。

心跳不该只报"我活着"，要报"我健康"—— 复用 `/health` 已经聚合的模块状态和
频道可达性。

### 第二层：基础设施探针

Uptime Kuma / 哪吒探针盯服务器的 HTTP/TCP 层。这一层跟 bot 无关，是主机监控。

### 第三层：端到端金丝雀 —— 不做

用测试账号定期给 bot 发 `/start` 断言收到回复。信号最强，但 **bot 不能给 bot
发消息**，需要 Telethon 挂一个真人账号（MTProto），有风控风险。

风险 > 收益。不做。

## 两个对我们无效的方案

写下来是为了不再重复评估：

**`getWebhookInfo` 轮询。** 我们用 long polling，返回的 `url` 是空字符串，
`last_error_message` 根本不存在 —— Telegram 侧没有任何投递记录，因为没有投递。
这个方案是 webhook bot 专属。

**`getMe` 当探针。** 它打的是 Telegram 服务器，只验证 token 有效，完全不反映
我们的进程死活。

## 告警通道：不能只用 Telegram

**用 Telegram 告警「Telegram bot 挂了」是循环依赖。** bot 挂掉很可能是因为
网络到 Telegram 断了，那告警也发不出去。

备用通道用 ntfy 或邮件。

## 性能：翻页慢是网络，不是代码

反复被问「为什么翻页慢，之前很快」。实测结论钉在这里，省得下次再纠结。

### 成本分解

一次翻页 = 本地检索 + 一次 `editMessageText` 往返：

| 环节 | 耗时 | 能不能改 |
|---|---|---|
| 检索（LIKE + 排序 + 分页） | ~28ms（最坏 `#漫改` 574 条） | 已是地板，不用改 |
| `editMessageText` 一次往返 | **~350ms** | 改不了，物理延迟 |
| 首次建连（TLS + 代理隧道） | ~2000ms，**一次性** | 启动预热已消除 |

所以稳态翻页 ≈ **380ms**。之前日志里出现的 2243ms 是冷启动第一次撞上建连。

### 那 350ms 是什么，为什么改不掉

实测 `getMe` 连续 5 次：首次 ~1700ms（建连），之后稳定 ~350ms（复用连接）。
这 350ms 是**本机到 Telegram 服务器（欧洲/新加坡）的物理往返**。

关键实测：**直连和走代理几乎一样**（直连 354ms / 代理 348ms）。而且这台机器
DNS 把 `api.telegram.org` 解析到 `198.18.0.35`（代理软件的 fake-ip），说明所谓
「直连」其实也被本地代理接管了。**去掉 `ANIMEBOT_PROXY` 不会让它变快。**

### 为什么「之前一点即达」

不是代码退化，是交互模型变了：

- 老架构：搜索 = 发一条静态消息，发完结束 = 一次往返。
- 现在：翻页 / 返回 = 每次再发一次 `editMessageText` = 每次一个 350ms 往返。

多了功能，每个功能都要过一次网络。这是新增交互的固有成本。

### 代码层面做过的两个优化

1. **启动预热**：见「启动」节，消除首次翻页的 ~2000ms 建连。
2. **`answer()` + `edit_text()` 并发**（`_safe_edit` 里的 `asyncio.gather`）：
   两个 API 无依赖，串行 700ms → 并发 350ms，省一整个往返，转圈也立刻停。
   注意：aiogram 的 `.answer()` 返回 TelegramMethod 对象（不是 coroutine），
   **必须先包成 coroutine 再进 gather**，否则 `unhashable type` 每次翻页必崩。
   回归测试 `test_safe_edit_uses_real_shortcut_methods` 盯着这条。

### 真正的解法：部署到能直连的海外 VPS

350ms 的地板只有换网络位置能打破。bot 跑在贴近 Telegram 服务器的海外 VPS 上，
往返回到 50~100ms，翻页才是真正的「一点即达」。在校园网 / 家宽本地跑，350ms
是物理下限，代码到头了。

部署后记得把 `slow_command_ms` 从 5000 调回 1500 —— 那个 5000 是迁就代理往返的，
直连环境下 1500 才能正常抓慢查询。

## 日常排查

### 用户报「出错了，编号 a1b2c3d4」

```bash
grep a1b2c3d4 logs/animebot.jsonl
```

一次请求的完整链路都带这个 trace_id。这是 8 位短 id 的全部意义 —— 用户能口述。

### 增量同步还在工作吗

```bash
uv run python -m animebot syncstat
```

或管理员发 `/syncstat`。两个数字要分开看：

- `最近收到` 不动 → **update 没到达**。查 bot 是不是还在频道里、还是管理员。
- `最近收到` 在动但 `最近入库` 不动 → 频道最近只发了公告，没发番剧帖。正常。

只看第二个数字的话，「一个月没发新番」和「同步彻底挂了」长得一模一样。

### `/q` 报「暂时用不了」或答「没查到」

按这个顺序：

1. `/health` —— `ai` 那一行是 🟢 还是 🔴（`status=unconfigured` 说明 key 没配）
2. `journalctl -u animebot | grep ai\.` —— 看是 `ai.http_error`（网关/额度）
   还是 `ai.tool_failed`（工具挂了，模型会照常回答但只能凭记忆）
3. `ai.tool{tool=web_search,outcome=error}` 一直涨 → Bing 改版或返回验证页，
   `web_search` 的 HTML 解析要跟着改
4. `ai.fallback` 出现 → 主模型（deepseek）在报错，已自动切到 glm

### 搜不到新发的帖子

按这个顺序查：

1. `/syncstat` —— update 到达了吗
2. `/health` —— 频道身份是 `administrator` 吗
3. `sync.parse_failed` 埋点（`/metrics`）—— 非 0 说明**频道模板变了**，
   需要往 [fields.py](../src/animebot/domain/fields.py) 补别名
4. 都正常 → 可能是停机期间漏的，重新导出 JSON 跑 `ingest`

### 改了解析器之后

```bash
uv run pytest tests/test_parser_golden.py
```

1639 个真实帖子是黄金数据集，`failed` 必须永远是 0，字段填充数不许下降。

重解析不需要重新导出 —— `raw_text` 全量保留在库里。

## 数据备份

只有一个文件：`data/animebot.db`（两个频道都在里面）。

但它**不是唯一真相** —— 导出 JSON 才是。库丢了从两份导出各重灌一次，upsert 幂等，
灌 10 次结果一样。所以备份优先级：导出 JSON > 数据库。

`.gitignore` 排除了 `data/`：几十 MB 的二进制进版本库，每次回填都产生一个
全量 diff。

## 关停

`Ctrl-C` 或 `SIGTERM`。`App.aclose()` 逆序 teardown 模块 → 关容器 → 关 bot
session。单个 teardown 失败只记日志不中断 —— 一个坏模块不能卡死整个关停。

**滚动部署要先停旧进程再起新的**，中间不能重叠 —— 否则就是上面那个 409。
