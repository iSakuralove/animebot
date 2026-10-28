"""回复与就地编辑的公共动作。翻页、落地页标签、群聊里的搜索结果都走这里。

放在 bot/ 而不是某个 feature 里：动漫和漫画两个模块都要用，而
features 之间不互相 import。
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, EphemeralMessageParameters, Message

from ..observability.logging import get_logger

log = get_logger("animebot.ui")


async def reply(message: Message, text: str, **kw: Any) -> None:
    """回复一条消息。群聊里优先 ephemeral —— 只有发送者自己看得到。

    两种情况直接交给 `Message.reply`：私聊、以及入站本身就是 ephemeral 消息
    （`ephemeral_message_id` 有值时 aiogram 会自动补 ephemeral 参数）。只有
    「群里的普通消息」（@bot 提及、手打的 `/s@bot`）需要显式指定接收者。

    群里的普通消息能不能私密回复，取决于 bot 在群里是不是管理员：是管理员随时
    可发，否则只能在收到触发动作后 15 秒内发、且必须带上触发动作的 id。这里
    没有触发动作，所以非管理员会失败 —— 失败就退回公开回复，功能降级而不报错。
    """

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply(text, **kw)
        return
    if message.ephemeral_message_id is not None or message.from_user is None:
        await message.reply(text, **kw)
        return

    ephemeral = EphemeralMessageParameters(receiver_user_id=message.from_user.id)
    try:
        await message.answer(text, ephemeral_message_parameters=ephemeral, **kw)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # 非管理员 / 老客户端 / 没有发 ephemeral 的权限：私密回复不可用，退回公开回复。
        # 只兜这两类 —— 网络错误时请求可能已经送达，再发一遍会把结果公开出去。
        log.info("reply.ephemeral_failed", chat_id=message.chat.id,
                 error=str(exc)[:120])
        await message.reply(text, **kw)


async def safe_edit(callback: CallbackQuery, text: str, **kw: object) -> None:
    """原地编辑并 answer。吞掉 "not modified"（重复点同一按钮），其余照抛。

    编辑而不是发新消息：翻页和进出详情都在同一条消息上完成，聊天记录不会被
    同一次搜索的十几个版本刷屏 —— 这是用户明确要的「就地修改」。

    群里的 ephemeral 结果要用另一套 API：`editMessageText` 的 message_id 对
    ephemeral 消息是 0，必须走 `editEphemeralMessageText`（带接收者）。这是
    Telegram 的硬约束，不是可选优化。
    """
    if callback.message is None:
        await callback.answer()
        return

    # answer() 和 edit() 无先后依赖，并发发：走代理时每次往返 ~350ms，
    # 串行是 700ms，并发是 350ms —— 少一整个往返，且转圈立刻停。
    #
    # 必须 await 成 coroutine 再交给 gather：aiogram 的 .answer()/.edit_text()
    # 返回的是 TelegramMethod 对象（pydantic model，可 await 但不是 coroutine），
    # 直接塞进 gather 会触发 "unhashable type" —— gather 拿它当 key 而 model 无 __hash__。
    async def _answer() -> None:
        await callback.answer()

    async def _edit() -> None:
        msg = callback.message
        ephemeral_id = getattr(msg, "ephemeral_message_id", None)
        if not ephemeral_id:
            await msg.edit_text(text, **kw)  # type: ignore[union-attr, arg-type]
            return
        bot = callback.bot or msg.bot
        receiver = getattr(msg, "receiver_user", None) or callback.from_user
        if bot is None or receiver is None:
            raise RuntimeError("ephemeral message has no bot/receiver context")
        await bot.edit_ephemeral_message_text(
            chat_id=msg.chat.id,
            receiver_user_id=receiver.id,
            ephemeral_message_id=ephemeral_id,
            text=text,
            **kw,
        )

    _, edit_exc = await asyncio.gather(_answer(), _edit(), return_exceptions=True)
    if isinstance(edit_exc, TelegramBadRequest):
        # 内容没变时 Telegram 报 not modified，是重复点击，吞掉。
        if "not modified" not in str(edit_exc).lower():
            raise edit_exc
    elif isinstance(edit_exc, BaseException):
        raise edit_exc
