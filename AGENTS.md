# AGENTS.md — 给后续 agent 的上手须知

这个文件是**公开仓库**的一部分，不写任何密钥、IP、token。环境相关的访问细节
（VPS 地址、SSH、systemd）在 gitignored 的 `CLAUDE.local.md` 里，本机 agent 能读到。

## 这是什么项目

Telegram 动漫频道帖子检索 bot。数据来自频道导出的 JSON（1600+ 帖），回填进
SQLite，用户在私聊里搜番、拿网盘链接。Bot API 读不到频道历史，所以是「导出
回填 + channel_post 增量同步」两条数据入口。

## 先读文档，别猜

- [docs/README.md](docs/README.md) — 文档索引，按「你想知道什么」查
- [docs/architecture.md](docs/architecture.md) — 分层、一次请求怎么走
- [docs/ADR/](docs/ADR/README.md) — 每个非显然的决策为什么这么定（改设计前先读）
- [docs/modules/](docs/modules/) — 各层契约与坑

代码回答「怎么做」，文档回答「为什么」。改之前先搞清为什么是现在这样。

## 铁律：测试和验证都在 VPS 上做

bot 跑在 VPS 上（直连 Telegram，比本地走代理快一倍）。**不要在本地起 bot** ——
本地和线上用同一个 token，两处 `getUpdates` 会 409 互踢，各拿一半消息，两边都坏。

- 要真机验证：改完 push，在 VPS 上 `git pull` + 重启，看 `journalctl`。
- 要先停线上：Telegram 里发 `/stop`（管理员），或 VPS 上 `systemctl stop animebot`。
- 具体 SSH / systemd 命令见 `CLAUDE.local.md`。

pytest 是纯本地的（用 `feed_update` 手搓 Update，不连 Telegram），本地随便跑。
只有「真机行为」这类验证才必须上 VPS。

## 开发命令（本地）

```bash
uv sync                              # 装依赖
uv run pytest -q                     # 全套测试
uv run ruff check src tests          # lint（提交前必过）
uv run animebot commands             # 看指令装配（不连网）
uv run animebot search "无职英雄"     # CLI 搜索（不连网，验检索逻辑）
uv run animebot run                  # 起 bot —— 只在确认线上已停时才在本地跑
```

## 改动纪律

- **提交前** `uv run pytest -q` 全绿 + `uv run ruff check src tests` 无告警。
- **提交信息写「为什么」**，不写「改了什么」——改了什么 `git show` 就能看到。
- **公开仓库**：提交前扫 token 泄漏（`.env` 已 gitignored，别把密钥写进代码/测试）。
- **加指令模块** = 建一个包 + 往 `ANIMEBOT_FEATURES` 加名字，不动 app.py。见
  [docs/adding_a_feature.md](docs/adding_a_feature.md)。
- 遵循 Linus 式取舍：消除特殊情况优于加分支，实测数据优于臆想，向后兼容是底线。

## 两个反复踩过的坑

1. **假夹具掩盖真 bug**：测试里把 aiogram 的方法换成 `async def` 假货，会让
   「gather 塞了非 coroutine」这类 bug 永远测不出来。测真实调用路径，别只测桩。
2. **本地默认值掩盖 env 解析**：pydantic-settings 对 tuple 字段会先 `json.loads`，
   本地没配 env 走默认值就发现不了，部署到真配 env 的机器才炸。配置改动要在
   「真的设了这个 env」的前提下验证。
