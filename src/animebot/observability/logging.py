"""日志。控制台给人看，文件给机器看（JSON Lines，一天一个文件）。

trace_id 由 processor 从 contextvars 自动注入 —— 业务代码写
`log.info("bgm.hit", subject_id=123)` 就够了，不必手动传 trace_id，
也就不会有人忘记传。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

from .context import current

_configured = False

# 从 token 里泄出去就麻烦了，日志里一律打码
_SECRET_KEYS = frozenset({"token", "bot_token", "api_key", "password", "authorization"})


def _inject_trace(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    if ctx := current():
        for k, v in ctx.as_log_fields().items():
            event.setdefault(k, v)
    return event


def _mask_secrets(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    for k in list(event):
        if k.lower() in _SECRET_KEYS and event[k]:
            event[k] = "***"
    return event


def _console_renderer(colors: bool) -> Any:
    return structlog.dev.ConsoleRenderer(
        colors=colors,
        # trace_id 和 command 提到最前面，扫日志时肉眼能对齐
        sort_keys=False,
    )


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    json_console: bool = False,
) -> None:
    """幂等。多次调用只生效第一次，测试里反复 setup 不会叠加 handler。"""
    global _configured
    if _configured:
        return

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_trace,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _mask_secrets,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(ensure_ascii=False)
                if json_console
                else _console_renderer(colors=sys.stderr.isatty()),
            ],
        )
    )
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        # 一天一个文件，留 14 天。出事故时按 trace_id grep 就完了。
        fileh = logging.handlers.TimedRotatingFileHandler(
            log_dir / "animebot.jsonl",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        fileh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.dict_tracebacks,
                    structlog.processors.JSONRenderer(ensure_ascii=False),
                ],
            )
        )
        root.addHandler(fileh)

    # aiogram 的 INFO 太吵，只要 warning
    for noisy in ("aiogram.event", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def reset_for_tests() -> None:
    global _configured
    _configured = False
