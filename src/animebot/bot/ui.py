"""回复、就地编辑、撤回的公共动作。翻页、群聊搜索、/q 的流式输出都走这里。

放在 bot/ 而不是某个 feature 里：动漫、漫画、AI 三个模块都要用，而
features 之间不互相 import。

**消息句柄用 `Sent` 而不是 `Message`。** 编辑要的只是 (chat, message_id)，
而 ephemeral 消息的 `message_id` 恒为 0，必须换成 (receiver_user_id,
ephemeral_message_id)。把这条分叉收在 `Sent` + `edit_text()` 里，调用方
（翻页、流式）不用各写一遍。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, EphemeralMessageParameters, Message

from ..observability.logging import get_logger

log = get_logger("animebot.ui")


@dataclass(frozen=True, slots=True)
class Sent:
    """一条已发出消息的句柄，够用来编辑和删除。"""

    chat_id: int
    message_id: int                  # ephemeral 消息恒为 0
    ephemeral_id: int | None = None
    receiver_id: int | None = None

    @classmethod
    def of(cls, msg: Message, receiver_id: int | None = None) -> Sent:
        return cls(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            ephemeral_id=getattr(msg, "ephemeral_message_id", None),
            receiver_id=receiver_id
            or getattr(getattr(msg, "receiver_user", None), "id", None),
        )


async def reply(
    message: Message, text: str, *, public: bool = False, **kw: Any
) -> Sent | None:
    """回复一条消息，返回句柄。群聊里默认走 ephemeral —— 只有发送者自己看得到。

    私聊直接 `Message.reply`。群里分两种：

    - `public=True`：**必须走 `answer`**。`Message.reply` 会从入站的
      `ephemeral_message_id` 自动补上 ephemeral 参数 —— 从群的 ephemeral 指令
      菜单发 `/q` 时就是这种情况，回复会跟着变成「仅自己可见」，即使调用方
      明确要公开。`answer` 没有这条继承规则。
    - 默认（`public=False`）：入站本身是 ephemeral 就直接 `reply`（继承是对的，
      只有提问者看得到）；否则显式指定接收者发 ephemeral。

    群里的普通消息能不能私密回复，取决于 bot 在群里是不是管理员：是管理员随时
    可发，否则只能在收到触发动作后 15 秒内发、且必须带上触发动作的 id。这里
    没有触发动作，所以非管理员会失败 —— 失败就退回公开回复，功能降级而不报错。

    `public=True` 是给 `/q` 这类「答案是全群的信息」用的。搜索结果相反 ——
    那是某个人在找东西，公开等于替他刷屏。
    """

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return _sent(await message.reply(text, **kw))
    if public:
        # 不用 reply：它会继承入站的 ephemeral（见 docstring）。公开消息也不该
        # 挂在对一条别人看不见的消息的回复链上。
        return _sent(await message.answer(text, **kw))
    if message.ephemeral_message_id is not None or message.from_user is None:
        return _sent(await message.reply(text, **kw))

    ephemeral = EphemeralMessageParameters(receiver_user_id=message.from_user.id)
    try:
        return _sent(await message.answer(text, ephemeral_message_parameters=ephemeral, **kw))
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # 非管理员 / 老客户端 / 没有发 ephemeral 的权限：私密回复不可用，退回公开回复。
        # 只兜这两类 —— 网络错误时请求可能已经送达，再发一遍会把结果公开出去。
        log.info("reply.ephemeral_failed", chat_id=message.chat.id,
                 error=str(exc)[:120])
        return _sent(await message.reply(text, **kw))


def _sent(msg: Any) -> Sent | None:
    return Sent.of(msg) if isinstance(msg, Message) else None


async def edit_text(bot: Bot, sent: Sent, text: str, **kw: Any) -> None:
    """编辑一条已发出的消息。ephemeral 走专用 API —— 它的 message_id 恒为 0。"""
    if sent.ephemeral_id:
        await bot.edit_ephemeral_message_text(
            chat_id=sent.chat_id,
            receiver_user_id=sent.receiver_id or 0,
            ephemeral_message_id=sent.ephemeral_id,
            text=text,
            **kw,
        )
    else:
        await bot.edit_message_text(
            chat_id=sent.chat_id, message_id=sent.message_id, text=text, **kw
        )


async def discard(bot: Bot, sent: Sent) -> None:
    """删掉自己发的消息。尽力而为 —— 删不掉（太旧、没权限）不影响主流程。"""
    try:
        if sent.ephemeral_id:
            await bot.delete_ephemeral_message(
                chat_id=sent.chat_id,
                receiver_user_id=sent.receiver_id or 0,
                ephemeral_message_id=sent.ephemeral_id,
            )
        else:
            await bot.delete_message(chat_id=sent.chat_id, message_id=sent.message_id)
    except Exception:
        log.debug("ui.discard_failed", chat_id=sent.chat_id, message_id=sent.message_id)


async def safe_edit(callback: CallbackQuery, text: str, **kw: object) -> None:
    """原地编辑并 answer。吞掉 "not modified"（重复点同一按钮），其余照抛。

    编辑而不是发新消息：翻页和进出详情都在同一条消息上完成，聊天记录不会被
    同一次搜索的十几个版本刷屏 —— 这是用户明确要的「就地修改」。
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
        bot = callback.bot or getattr(msg, "bot", None)
        if bot is None:
            raise RuntimeError("callback has no bot context")
        receiver = getattr(msg, "receiver_user", None) or callback.from_user
        if receiver is None:
            raise RuntimeError("ephemeral message has no receiver")
        await edit_text(
            bot, Sent.of(msg, receiver.id), text, **kw  # type: ignore[arg-type]
        )

    _, edit_exc = await asyncio.gather(_answer(), _edit(), return_exceptions=True)
    if isinstance(edit_exc, TelegramBadRequest):
        # 内容没变时 Telegram 报 not modified，是重复点击，吞掉。
        if "not modified" not in str(edit_exc).lower():
            raise edit_exc
    elif isinstance(edit_exc, BaseException):
        raise edit_exc
