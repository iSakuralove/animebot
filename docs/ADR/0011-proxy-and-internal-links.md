# 0011. 走代理连 Telegram，深链默认 internal

状态: 已采纳
日期: 2026-09-19

## 背景

两个独立的现实约束在这次同时落地：

1. **开发机直连 `api.telegram.org` 被墙。** aiohttp 直连超时（WinError 121
   信号灯超时），而系统浏览器 / PowerShell 能通（它们走系统代理）。aiogram 的
   aiohttp session 默认不读系统代理，于是 bot 起不来。
2. **资源是频道福利。** 深链之前默认 `public`（`t.me/YXHMd/<id>`，任何人可点）。
   但下载资源本就是给频道成员的，公开链接对非成员点开也是 404 / 加入页。

## 决策

- 新增 `ANIMEBOT_PROXY` 配置。非空时给 `Bot` 传
  `AiohttpSession(proxy=...)`；空则直连。依赖 `aiohttp-socks`（aiogram 走代理
  的前置库，不装会在构造 session 时抛 RuntimeError）。
- `link_mode` 默认从 `public` 改为 `internal`（`t.me/c/<channel_id>/<id>`）。

## 理由

**代理必须显式传。** aiohttp 不像 requests/urllib 那样默认读 `HTTP_PROXY`
环境变量，所以「系统能连 = Python 能连」不成立。把代理做成一个配置项而不是
写死，是因为部署到能直连的服务器时要能一键关掉（留空即可）。

**internal 是正确的默认。** 频道是 `public_channel`，`t.me/c/` 私有链接只有
已加入的账号能打开 —— 这正好匹配「资源给成员」的语义。想要公开分享的部署把
`ANIMEBOT_LINK_MODE=public` 打开即可，开关还在。

## 代价

- **代理是延迟来源。** 每次 `sendMessage` / `editMessageText` 都绕代理 2~3 秒
  往返，翻页体感明显变慢。检索本身仍是 ~10ms（`handler.slow` 埋点上看得到
  `纯检索 10ms / handler 2243ms` 的差值）。这不是代码能修的 —— 根治靠部署到
  能直连 Telegram 的机器，届时 `ANIMEBOT_PROXY` 留空，回到「一点即达」。
- **`slow_command_ms=1500` 在代理下每次误报。** 阈值是按直连设的。暂不调高，
  因为它如实反映了「用户等了多久」；直连后自然不再触发。
- **internal 深链的 link preview 不出图。** `t.me/c/` 私有链接 Telegram 抓不到
  预览图，所以 `/check` 详情在 internal 模式下没有头图。消息本身正常 —— 优雅
  降级，不为它加分支。public 模式下预览正常。
- **aiohttp-socks 是新依赖。** 直连部署其实用不到它，但装着无害（几十 KB），
  换来「改一个环境变量就能在墙内墙外切换」，值。
