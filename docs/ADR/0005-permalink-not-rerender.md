# 0005. 搜索结果给原帖深链，不重新渲染帖子

状态: 已采纳
日期: 2026-09-05

## 背景

搜到一个帖子后，要怎么展示给用户？帖子原本长这样：一张封面图 + 结构化文本 +
（部分帖子有）三个 inline 按钮【百度链接】【谷歌表格】【OD节点】。

三条路：

| 方式 | 图片 | 原帖按钮 | 代价 |
|---|---|---|---|
| 深链 `t.me/<username>/<id>` | ✅ 点进去就有 | ✅ | 零 |
| `copyMessage` | ✅ 服务端复制 | ❌ 要自己传 `reply_markup` | bot 必须在频道内 |
| 自己重渲染 | ❌ 没有 file_id | 要重建 | 一整套渲染管线 |

## 决策

**结果列表给 `t.me/<username>/<message_id>` 深链**，详情页额外给各网盘的
URL 按钮。不做 `copyMessage`，不重渲染帖子。

链接形式有总开关 `ANIMEBOT_LINK_MODE`：

- `public` → `https://t.me/YXHMd/3948`（任何人可点）
- `internal` → `https://t.me/c/1702674582/3948`（仅频道成员可见）

## 理由

**自己渲染拿不到图片。** 导出格式里没有 `file_id`（只有本地文件路径），而
`file_id` 是 per-bot 凭证 —— 别的 bot 的用不了。要拿到图片只能 `copyMessage`
或让用户点进原帖。

**重渲染必然与原帖不一致。** 帖子里的格式（bold 标题、code 密码块、custom_emoji）
在导出 JSON 里是 entity，重建成 HTML 会丢失或错位。而且解压密码、三个网盘链接
都得重新处理一遍 —— 这些逻辑已经在解析器里做过一次了，再做一次就有两份真相。

**`(channel_id, message_id)` 就是整条原帖的完整引用。** 图片、按钮、排版都在
Telegram 服务器上。一行 URL 顶掉整套渲染管线。

**帖子自己就写了要用频道链接。** 每个新帖底部有这句：

> 请不要在讨论中打开链接，请使用频道消息的链接或者表格，讨论中的链接是失效的

跳原帖跟这个既有约定一致。

**`copyMessage` 不带原按钮，但这不重要。** 只有 129/1639（8.7%）的帖子有
inline 按钮，而那些按钮的 URL 已经被解析进 `post.links` 了，需要时重建即可
（`detail_keyboard()` 就是这么做的）。

## 代价

**用户要多点一次。** 结果列表里是链接，点进去才看到图和完整内容。缓解：
详情页（点结果序号按钮）直接给了评分/话数/staff/简介摘要 + 各网盘直链按钮，
大部分情况不用真的跳频道。

**`internal` 模式的链接只对频道成员有效。** 这是 Telegram 的限制，不是 bug。
默认 `public` 就是为了避开它。

**bot 完全不需要加入频道** —— 这是意外收益。深链是纯字符串拼接，不调 API。
（增量同步需要 bot 在频道内，但那是另一件事。）

## 顺带：callback_data 只放整数

结果列表的按钮 `callback_data` 是 `p:<message_id>`，不是 URL。

Telegram 的 `callback_data` 上限 **64 字节**。真实数据里的 sharepoint 链接：

```
https://p42k-my.sharepoint.com/:f:/g/personal/yuexiaheimao_p42k_onmicrosoft_com/Ej0MwyQSpFtAo2Z0Nf832WwB-4AaqPxof54xC-U7W40jKg?e=XLdPO2
```

200+ 字符，塞进去必爆。旧代码写的是 `callback_data=f"detail|{href}"`。

网盘直链走 **URL 按钮**（`InlineKeyboardButton(url=...)`），那种不占
`callback_data` 配额。

## 相关

- [modules/search.md](../modules/search.md) —— presenter 的渲染契约
