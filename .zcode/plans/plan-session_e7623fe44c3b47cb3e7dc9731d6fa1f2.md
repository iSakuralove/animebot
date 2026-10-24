# 搜索结果页加「漫画 / 动漫」切换按钮

## 核心设计（零新概念）

切换按钮 = **对方频道编解码器编码的「第 0 页」翻页按钮**。点击后由对方模块已有的
`on_page` 处理器接住（路由过滤器 `F.data.func(codec.owns)` 已经按前缀分流），
用同一查询词重查、`safe_edit` 原地换掉整条消息。不新增 callback 格式、不新增
处理器、不新增路由 —— 「切换」只是「跨频道翻到第 0 页」。

- 动漫结果页：翻页行**上方**加一行 `📚 漫画`（callback = `c:0:l:<查询词>`）
- 漫画结果页：对称地加 `🎬 动漫`（callback = `s:0:l:<查询词>`），否则切过去就回不来了
- 长查询词自动走现有 token 路径（`ct:` / `t:`），编解码已处理
- 切换一律用宽松模式（漫画只有宽松入口；回来也按纯文本语义）

## 显示时机

| 结果页状态 | 键盘 |
|---|---|
| 空结果（没找到） | 只有切换行 ——「动漫没有 → 试试漫画」正是最有用的场景 |
| 单页结果 | 只有切换行（截图里无职英雄 1 条命中就是这种） |
| 多页结果 | 切换行在上，翻页行在下（`inline_keyboard[-1]` 仍是翻页行，现有断言不破） |

对方模块未启用时（`settings.features` 里没有）不显示按钮 —— 否则点了个没人接的
callback，客户端会一直转圈。`/check` 详情、`/c` 落地页不加（没有查询词或语义不符）。

## 改动文件

1. `src/animebot/search/presenter.py`
   - `page_keyboard(page, store, codec=ANIME_PAGES, *, switch=None)`：`switch`
     非空时先加切换行；多页且非空才加翻页行。`switch=None` 时行为与现在完全一致
     （老调用、老测试不受影响）。
   - 新增 `manga_switch_button(page, store)` / `anime_switch_button(page, store)`
     两个小工厂。
2. `src/animebot/features/search/handlers.py`：`_reply_search` 和 `on_page` 传
   `switch=manga_switch_button(...) if "manga" in settings.features else None`。
3. `src/animebot/features/manga/handlers.py`：`_present` 传
   `switch=anime_switch_button(...) if "search" in settings.features else None`。
4. `callbacks.py` 不动。

## 测试（test_manga_feature.py 为主）

- 更新 `test_c_finds_manga_with_files`：单页漫画结果的 markup 从 `None` 变为
  切换行（该断言会被本次改动打破，属预期）。
- 新增：
  - 动漫结果（Dispatcher 走 `/s` / 纯文本）→ markup 首行是 `📚 漫画`，点
    `c:0:l:<q>` 后消息被编辑成漫画结果（带 📚 头）
  - 单条命中（截图场景）：只有切换行、无翻页行
  - 空结果：仍带切换行；点击后到漫画空结果，且有 `🎬 动漫` 可回
  - 漫画结果页点 `🎬 动漫` 编辑回动漫结果
  - 多页时翻页行仍是最后一行（宽度不变的老约束）
  - `settings.features` 不含对方模块 → 无切换按钮、单页 markup 为 None
  - 长查询词切换走 `ct:`/`t:` token 路径可往返
- 现有 `test_pagination*.py` 直呼 `page_keyboard(...)` 不传 switch → 行为不变，应全绿。

## 文档

- `docs/design/manga-search.md`：加「切换」一节（跨频道第 0 页按钮、显示时机、
  依赖对方模块加载）。
- `docs/modules/search.md` 键盘节、README 指令表、PRD 已交付表：各加一句。

## 提交与部署

提交（信息写为什么：默认入口仍动漫，切换是用户主动行为）→ push → VPS
`git pull` + 重启（无依赖、无迁移）→ `journalctl` 验证启动；本地 CLI 无法验证
按钮交互，以 Dispatcher 级测试为准。