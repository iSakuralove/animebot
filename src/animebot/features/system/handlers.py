"""系统指令。全部只读，不碰业务数据。

/help 从 CommandRegistry 生成 —— 加了新指令帮助自动就有，不存在忘记更新。
"""

from __future__ import annotations

import html
import platform
import sys
import time

from aiogram.types import Message

from ... import __version__
from ..._util import command_arg, human_duration
from ...config import Settings
from ...core.errors import NotFound
from ...core.registry import CommandRegistry, command
from ...observability.context import RequestContext
from ...observability.metrics import METRICS
from ...storage.repo import PostRepo

_BOOT = time.time()


@command(
    "help",
    desc="查看所有指令",
    usage="/help 或 /help search",
    aliases=("start", "h"),
    long_help="不带参数列出全部指令；带指令名看该指令的详细用法。",
)
async def cmd_help(message: Message, registry: CommandRegistry) -> None:
    arg = command_arg(message)

    if arg:
        spec = registry.get(arg)
        if spec is None:
            raise NotFound(f"指令 /{html.escape(arg)}")
        lines = [f"<b>/{spec.name}</b> — {html.escape(spec.desc or '（无说明）')}"]
        if spec.aliases:
            lines.append("别名: " + ", ".join(f"/{a}" for a in spec.aliases))
        lines.append(f"用法: <code>{html.escape(spec.usage)}</code>")
        if spec.long_help:
            lines.append("")
            lines.append(html.escape(spec.long_help))
        if spec.rate:
            lines.append(f"\n限流: {spec.rate.per_seconds:.0f} 秒内 {spec.rate.times} 次")
        await message.reply("\n".join(lines))
        return

    blocks: list[str] = ["<b>可用指令</b>"]
    for feature, specs in registry.by_feature().items():
        visible = [s for s in specs if not s.hidden and not s.admin_only]
        if not visible:
            continue
        blocks.append(f"\n<i>{html.escape(feature)}</i>")
        blocks.extend(
            f"/{s.name} — {html.escape(s.desc or '')}" for s in visible
        )
    blocks.append("\n<code>/help &lt;指令&gt;</code> 看详细用法")
    await message.reply("\n".join(blocks))


@command("ping", desc="测活", rate=(5, 10))
async def cmd_ping(message: Message, trace: RequestContext) -> None:
    await message.reply(
        f"pong <code>{trace.elapsed_ms:.0f}ms</code>\ntrace: <code>{trace.trace_id}</code>"
    )


@command("trace", desc="显示本次请求的追踪信息", hidden=True)
async def cmd_trace(message: Message, trace: RequestContext) -> None:
    fields = "\n".join(f"{k}: <code>{html.escape(str(v))}</code>"
                       for k, v in trace.as_log_fields().items())
    await message.reply(f"<b>请求上下文</b>\n{fields}")


@command("health", desc="健康检查", admin_only=True)
async def cmd_health(
    message: Message,
    repo: PostRepo,
    settings: Settings,
    registry: CommandRegistry,
) -> None:
    breakdown = await repo.status_breakdown()
    lines = [
        "<b>健康状况</b>",
        f"版本: <code>{__version__}</code>",
        f"运行: {human_duration(time.time() - _BOOT)}",
        f"Python: <code>{sys.version.split()[0]}</code> / {platform.system()}",
        f"帖子: <b>{await repo.count()}</b>  {breakdown}",
        f"指令: {len(registry)}",
        f"频道: <code>{settings.channel_id}</code> "
        f"({settings.link_mode} / @{settings.channel_username})",
        f"数据库: <code>{html.escape(str(settings.db_path))}</code>",
    ]
    await message.reply("\n".join(lines))


@command("metrics", desc="运行指标", admin_only=True)
async def cmd_metrics(message: Message) -> None:
    snap = METRICS.snapshot()
    lines = [f"<b>运行指标</b>（{human_duration(snap['uptime_s'])}）", "", "<b>计数</b>"]
    counters = snap["counters"]
    if counters:
        lines.extend(
            f"<code>{html.escape(k)}</code> = {v}" for k, v in list(counters.items())[:25]
        )
    else:
        lines.append("（暂无）")

    lines.append("\n<b>耗时</b>")
    timings = snap["timings"]
    if timings:
        for k, v in list(timings.items())[:15]:
            lines.append(
                f"<code>{html.escape(k)}</code>\n"
                f"  n={v['count']} avg={v['avg_ms']}ms "
                f"p95={v['p95_ms']}ms max={v['max_ms']}ms"
            )
    else:
        lines.append("（暂无）")
    await message.reply("\n".join(lines))
