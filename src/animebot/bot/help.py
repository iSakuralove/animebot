"""指令清单的渲染。

`/help` 和群里的 `@bot`（不带关键词）共用一份 —— 两份列表必然会漂移，而
「菜单里有、真发指令说不认识」是最难查的一类不一致。
"""

from __future__ import annotations

import html

from ..core.registry import CommandRegistry


def render_command_list(registry: CommandRegistry, *, is_admin: bool) -> str:
    """按模块分组的指令清单。hidden 对谁都不列；admin_only 只对管理员列。"""
    blocks: list[str] = ["<b>可用指令</b>"]
    for feature, specs in registry.by_feature().items():
        shown = [
            s for s in specs
            if not s.hidden and (is_admin or not s.admin_only)
        ]
        if not shown:
            continue
        blocks.append(f"\n<i>{html.escape(feature)}</i>")
        blocks.extend(
            ("🔒 " if s.admin_only else "") + f"/{s.name} — {html.escape(s.desc or '')}"
            for s in shown
        )
    return "\n".join(blocks)
