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
from ...bot.help import render_command_list
from ...bot.ui import reply
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
async def cmd_help(
    message: Message, registry: CommandRegistry, is_admin: bool = False
) -> None:
    """is_admin 由 AccessMiddleware 从 admin_ids 算好注入 —— 管理员额外看到
    🔒 标记的专用指令，普通用户的列表里它们不存在。

    群里回给别人看没用，走 bot.ui.reply（有 ephemeral 条件就私密）。
    """
    arg = command_arg(message)

    if arg:
        spec = registry.get(arg)
        if spec is None:
            raise NotFound(f"指令 /{html.escape(arg)}")
        title = f"/{spec.name}"
        if spec.admin_only:
            title += "（管理员专用）"
        lines = [f"<b>{title}</b> — {html.escape(spec.desc or '（无说明）')}"]
        if spec.aliases:
            lines.append("别名: " + ", ".join(f"/{a}" for a in spec.aliases))
        lines.append(f"用法: <code>{html.escape(spec.usage)}</code>")
        if spec.long_help:
            lines.append("")
            lines.append(html.escape(spec.long_help))
        if spec.rate:
            lines.append(f"\n限流: {spec.rate.per_seconds:.0f} 秒内 {spec.rate.times} 次")
        await reply(message, "\n".join(lines))
        return

    body = (
        render_command_list(registry, is_admin=is_admin)
        + "\n\n<code>/help &lt;指令&gt;</code> 看详细用法"
    )
    await reply(message, body)


# 运维/探测指令只对管理员开放：它们回答的是「部署健不健康」，不是用户功能。
# 普通用户知道 bot 活没活的方式就是直接搜一下 —— 搜索本身有限流，不怕刷。
@command("ping", desc="测活", rate=(5, 10), admin_only=True)
async def cmd_ping(message: Message, trace: RequestContext) -> None:
    await reply(message, 
        f"pong <code>{trace.elapsed_ms:.0f}ms</code>\ntrace: <code>{trace.trace_id}</code>"
    )


@command("trace", desc="显示本次请求的追踪信息", hidden=True, admin_only=True)
async def cmd_trace(message: Message, trace: RequestContext) -> None:
    fields = "\n".join(f"{k}: <code>{html.escape(str(v))}</code>"
                       for k, v in trace.as_log_fields().items())
    await reply(message, f"<b>请求上下文</b>\n{fields}")


@command("health", desc="健康检查", admin_only=True)
async def cmd_health(
    message: Message,
    repo: PostRepo,
    settings: Settings,
    registry: CommandRegistry,
    app: object = None,
) -> None:
    lines = [
        "<b>健康状况</b>",
        f"版本: <code>{__version__}</code>",
        f"运行: {human_duration(time.time() - _BOOT)}",
        f"Python: <code>{sys.version.split()[0]}</code> / {platform.system()}",
        f"指令: {len(registry)}",
        f"深链: {settings.link_mode}",
        f"数据库: <code>{html.escape(str(settings.db_path))}</code>",
        "\n<b>频道</b>",
    ]
    for c in settings.channels:
        cid = c.channel_id
        posts = await repo.count(channel_id=cid)
        files = await repo.file_count(channel_id=cid)
        breakdown = await repo.status_breakdown(channel_id=cid)
        at = f" @{html.escape(c.username)}" if c.username else ""
        lines.append(
            f"{html.escape(c.title)} <code>{cid}</code>{at}: 帖子 <b>{posts}</b>"
            f" · 文件 {files}  {breakdown}"
        )

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
    access_by_channel = getattr(app, "channel_access", None) or {}
    if access_by_channel:
        lines.append("\n<b>增量同步</b>")
        for c in settings.channels:
            access = access_by_channel.get(c.key)
            if access is None:
                continue
            mark = "🟢" if access.ok else "🔴"
            lines.append(
                f"{mark} {html.escape(c.title)}频道身份: <code>{access.status}</code>"
            )
            if not access.ok:
                lines.append(html.escape(access.hint))

    await reply(message, "\n".join(lines))


@command("syncstat", desc="增量同步水位", admin_only=True)
async def cmd_syncstat(message: Message, sync: ChannelSync, settings: Settings) -> None:
    """回答两个问题：update 还在到达吗？索引更新到哪了？每个频道各答一遍。

    这两个必须分开看 —— 只看「索引更新到哪」的话，一个月没发新番和同步彻底
    挂掉长得一模一样。
    """
    blocks = ["<b>增量同步</b>"]
    for c in settings.channels:
        r = await sync.gap_report(channel=c)
        head = f"\n<b>{html.escape(c.title)}</b> <code>{c.channel_id}</code>"
        if not r["seen_count"]:
            blocks.append(
                f"{head}\n还没收到过这个频道的消息。要么频道确实没发新帖，"
                "要么 bot 不是频道管理员 —— 用 /health 看频道身份。"
            )
            continue
        blocks.append(
            f"{head}\n"
            f"最近收到: <code>#{r['last_seen_message_id']}</code> "
            f"{html.escape(r['last_seen_at'])}\n"
            f"最近入库: <code>#{r['db_max_message_id']}</code> "
            f"{html.escape(r['last_stored_at'] or '—')}\n"
            f"累计: 收到 {r['seen_count']} 条 / 入库 {r['stored_count']} 条"
        )
    await reply(message, "\n".join(blocks))


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
    await reply(message, "\n".join(lines))
