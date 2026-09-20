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
from ...ingest.sync import ChannelSync
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
    app: object = None,
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

    # 各模块自报状态。BaseFeature.health() 定义了这个钩子，这里是唯一的读取点
    # —— 不聚合的话那个钩子等于不存在。
    features = getattr(app, "features", None) or []
    if features:
        lines.append("\n<b>模块</b>")
        for f in features:
            try:
                info = f.health()
            except Exception as exc:
                # 一个模块坏了不该让 /health 整个挂掉 —— 那正是最需要它的时候
                info = {"status": "error", "error": type(exc).__name__}
            mark = "🟢" if info.get("status") == "ok" else "🔴"
            detail = "  ".join(
                f"{k}={html.escape(str(v))}" for k, v in info.items() if k != "status"
            )
            lines.append(f"{mark} <code>{f.name}</code> {detail}".rstrip())

    # 频道可达性：bot 不是管理员就收不到 channel_post，而那个失败完全静默
    access = getattr(app, "channel_access", None)
    if access is not None:
        mark = "🟢" if access.ok else "🔴"
        lines.append(f"\n<b>增量同步</b>\n{mark} 频道身份: <code>{access.status}</code>")
        if not access.ok:
            lines.append(html.escape(access.hint))

    await message.reply("\n".join(lines))


@command("syncstat", desc="增量同步水位", admin_only=True)
async def cmd_syncstat(message: Message, sync: ChannelSync, settings: Settings) -> None:
    """回答两个问题：update 还在到达吗？索引更新到哪了？

    这两个必须分开看 —— 只看「索引更新到哪」的话，一个月没发新番和同步彻底
    挂掉长得一模一样。
    """
    r = await sync.gap_report()
    if not r["seen_count"]:
        await message.reply(
            "<b>增量同步</b>\n还没收到过任何频道消息。\n\n"
            "要么频道确实没发新帖，要么 bot 不是频道管理员 —— 用 /health 看频道身份。"
        )
        return
    lines = [
        "<b>增量同步</b>",
        f"最近收到: <code>#{r['last_seen_message_id']}</code> "
        f"{html.escape(r['last_seen_at'])}",
        f"最近入库: <code>#{r['db_max_message_id']}</code> "
        f"{html.escape(r['last_stored_at'] or '—')}",
        f"累计: 收到 {r['seen_count']} 条 / 入库 {r['stored_count']} 条",
        f"频道: <code>{settings.channel_id}</code>",
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
