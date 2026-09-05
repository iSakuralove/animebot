"""小工具：指令参数提取、时长格式化。没有归属的公共函数放这里。"""

from __future__ import annotations

from aiogram.types import Message


def command_arg(message: Message) -> str:
    """取 '/search 无职英雄' 里的 '无职英雄'。没有参数返回空串。"""
    text = message.text or message.caption or ""
    _, _, tail = text.partition(" ")
    return tail.strip()


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60}秒"
    if seconds < 86400:
        return f"{seconds // 3600}小时{seconds % 3600 // 60}分"
    return f"{seconds // 86400}天{seconds % 86400 // 3600}小时"
