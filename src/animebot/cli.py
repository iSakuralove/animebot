"""命令行入口。回填、看库状态、不起 bot 直接搜 —— 调试全靠它。

    python -m animebot ingest "C:\\...\\频道json数据"
    python -m animebot stats
    python -m animebot syncstat
    python -m animebot search 无职英雄
    python -m animebot search "#奇幻 #异世界"
    python -m animebot commands
    python -m animebot run
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings
from .ingest.export_loader import ExportMismatch, ingest_export
from .ingest.sync import ChannelSync
from .observability.context import request_context
from .observability.logging import setup_logging
from .search.service import SearchService
from .storage.repo import PostRepo


async def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = get_settings()
    async with PostRepo(cfg.db_path) as repo:
        await repo.init_schema()
        with request_context(command="ingest") as ctx:
            try:
                stats = await ingest_export(
                    args.path,
                    repo,
                    expected_channel_id=None if args.force else cfg.channel_id,
                    trace_id=ctx.trace_id,
                )
            except ExportMismatch as exc:
                print(f"拒绝导入: {exc}", file=sys.stderr)
                return 2
        print(
            f"消息 {stats.total}  ok {stats.ok}  partial {stats.partial}  "
            f"failed {stats.failed}  skipped {stats.skipped}  写库 {stats.upserted}"
        )
    return 0


async def cmd_stats(_args: argparse.Namespace) -> int:
    cfg = get_settings()
    async with PostRepo(cfg.db_path) as repo:
        await repo.init_schema()
        print(f"db        : {cfg.db_path}")
        print(f"帖子      : {await repo.count()}")
        print(f"解析状态  : {await repo.status_breakdown()}")
        tags = await repo.tag_cloud(limit=15)
        print("热门标签  : " + "  ".join(f"#{t}({n})" for t, n in tags))
        idx = await repo.tag_cloud("index", limit=10)
        print("引索      : " + "  ".join(f"#{t}({n})" for t, n in idx))

        state = await repo.sync_state(cfg.channel_id)
        if state:
            print(
                f"增量同步  : 最近收到 #{state['last_seen_message_id']} "
                f"({state['last_seen_at']}) / "
                f"收到 {state['seen_count']} 入库 {state['stored_count']}"
            )
        else:
            print("增量同步  : 还没收到过频道消息")
    return 0


async def cmd_syncstat(_args: argparse.Namespace) -> int:
    """诊断同步是否还在工作。不连 Telegram，只读库里的水位。"""
    cfg = get_settings()
    async with PostRepo(cfg.db_path) as repo:
        await repo.init_schema()
        sync = ChannelSync(repo, cfg)
        r = await sync.gap_report()
        print(f"频道          : {cfg.channel_id} (@{cfg.channel_username})")
        print(f"库里最大 id   : #{r['db_max_message_id']}")
        print(f"最近收到      : #{r['last_seen_message_id']}  {r['last_seen_at'] or '—'}")
        print(f"最近入库时间  : {r['last_stored_at'] or '—'}")
        print(f"累计          : 收到 {r['seen_count']}  入库 {r['stored_count']}")
        if not r["seen_count"]:
            print(
                "\n还没收到过任何频道消息。要么频道确实没发新帖，"
                "要么 bot 不是频道管理员 —— 后者收不到 channel_post 且完全静默。"
            )
            return 1
    return 0


async def cmd_search(args: argparse.Namespace) -> int:
    cfg = get_settings()
    query = " ".join(args.query)
    async with PostRepo(cfg.db_path) as repo:
        svc = SearchService(repo, cfg)
        page = await svc.search_page(query, page=args.page, page_size=args.limit)
        if page.is_empty:
            print(f"没找到 {query!r}")
            return 1
        last = page.first_index + len(page.hits) - 1
        print(
            f"{query!r} -> 共 {page.total} 条"
            f"（{page.first_index}-{last}，第 {page.page + 1}/{page.pages} 页）\n"
        )
        for i, h in enumerate(page.hits):
            p = h.post
            score = f"{p.score:.1f}" if p.score is not None else "--"
            print(f"{page.first_index + i}. {p.title_cn}")
            print(
                f"   评分 {score} {p.score_text} | {p.episodes or '?'}话 | "
                f"{p.air_date or '?'} | #{' #'.join(p.tags[:5])}"
            )
            print(f"   {p.permalink(cfg.link_username)}")
            print(
                f"   [{h.reason} 分{h.score:.0f} 命中{h.hits}词]  "
                f"网盘: {', '.join(p.links) or '无'}"
            )
            print()
        if page.has_next:
            print(f"下一页: --page {page.page + 1}")
    return 0


async def cmd_commands(_args: argparse.Namespace) -> int:
    """列出所有已注册指令。加了新模块用这个确认装配成功，不必真连 Telegram。"""
    from .bot.app import build_app

    setup_logging(level="WARNING")
    app = await build_app(with_bot=False)
    try:
        for feature, specs in app.registry.by_feature().items():
            print(f"\n[{feature}]")
            for s in specs:
                flags = "".join(
                    (
                        "A" if s.admin_only else "",
                        "H" if s.hidden else "",
                        "R" if s.rate else "",
                    )
                )
                alias = f" ({', '.join('/' + a for a in s.aliases)})" if s.aliases else ""
                print(f"  /{s.name}{alias}  {flags:<3} {s.desc}")
        print(f"\n共 {len(app.registry)} 条指令")
    finally:
        await app.aclose()
    return 0


async def cmd_run(_args: argparse.Namespace) -> int:
    from .bot.runner import run_polling

    return await run_polling()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="animebot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="从 Telegram Desktop 导出的 result.json 回填")
    p.add_argument("path", help="result.json 或它所在的目录")
    p.add_argument("--force", action="store_true", help="跳过频道 id 校验（危险）")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("stats", help="看库状态")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("syncstat", help="增量同步水位（诊断同步是否还在工作）")
    p.set_defaults(fn=cmd_syncstat)

    p = sub.add_parser("search", help="命令行搜索")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", "--limit", type=int, default=8, help="每页条数")
    p.add_argument("--page", type=int, default=0, help="页码（0-based）")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("commands", help="列出所有已注册指令（不连 Telegram）")
    p.set_defaults(fn=cmd_commands)

    p = sub.add_parser("run", help="启动 bot（long polling）")
    p.set_defaults(fn=cmd_run)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
