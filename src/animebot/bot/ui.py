"""就地编辑消息的公共动作。翻页、从落地页点标签，都走这里。

放在 bot/ 而不是某个 feature 里：动漫和漫画两个模块都要用，而
features 之间不互相 import。
"""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery


async def safe_edit(callback: CallbackQuery, text: str, **kw: object) -> None:
    """原地编辑并 answer。吞掉 "not modified"（重复点同一按钮），其余照抛。

    编辑而不是发新消息：翻页和进出详情都在同一条消息上完成，聊天记录不会被
    同一次搜索的十几个版本刷屏 —— 这是用户明确要的「就地修改」。
    """
    if callback.message is None:
        await callback.answer()
        return

    # answer() 和 edit_text() 无先后依赖，并发发：走代理时每次往返 ~350ms，
    # 串行是 700ms，并发是 350ms —— 少一整个往返，且转圈立刻停。
    #
    # 必须 await 成 coroutine 再交给 gather：aiogram 的 .answer()/.edit_text()
    # 返回的是 TelegramMethod 对象（pydantic model，可 await 但不是 coroutine），
    # 直接塞进 gather 会触发 "unhashable type" —— gather 拿它当 key 而 model 无 __hash__。
    async def _answer() -> None:
        await callback.answer()

    async def _edit() -> None:
        await callback.message.edit_text(text, **kw)  # type: ignore[union-attr, arg-type]

    _, edit_exc = await asyncio.gather(_answer(), _edit(), return_exceptions=True)
    if isinstance(edit_exc, TelegramBadRequest):
        # 内容没变时 Telegram 报 not modified，是重复点击，吞掉。
        if "not modified" not in str(edit_exc).lower():
            raise edit_exc
    elif isinstance(edit_exc, BaseException):
        raise edit_exc
