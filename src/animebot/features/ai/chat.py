"""一次 /q 的编排：会话 → 模型 → 工具 → 再问 → 最终回答。

抽出来单独一个文件，是为了能在没有 Telegram 的情况下测整条链路
（`client` 和 `toolbox` 都可以换成假的）。handlers.py 只负责
「取参数、流式推草稿、发最终消息」这些 Telegram 相关的事。

工具循环的形状（Claude 协议）：

    user: 问题
    assistant: [text?, tool_use...]     stop_reason=tool_use
    user: [tool_result...]              ← 工具结果以 user 角色回灌
    assistant: [text...]                stop_reason=end_turn

两个必须处理的边界：

1. **轮数上限。** 模型不会自己停。到上限时把最后一条 user 消息换成
   「别再调工具了，用已有信息回答」，逼它收尾 —— 直接截断会让用户拿到
   半个答案。
2. **工具失败不中断。** `ToolBox.run` 把异常转成 `{"error": ...}`，模型
   看到错误会自己换策略。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...config import Settings
from ...core.errors import ExternalServiceError
from ...observability.logging import get_logger
from ...observability.metrics import METRICS
from .client import ClaudeClient, Stopped
from .session import Conversation
from .tools import ToolBox

log = get_logger("animebot.ai.chat")

# 工具名 -> 给用户看的动作提示。模型不该说「我调用了一个函数」，
# 而用户也需要知道那几秒在等什么。
TOOL_LABELS = {
    "bangumi_search": "🔍 正在查 Bangumi",
    "bangumi_detail": "📖 正在看条目详情",
    "bangumi_browse": "📊 正在翻 Bangumi 榜单",
    "web_search": "🌐 正在联网搜索",
    "channel_search": "📚 正在查频道资源",
}

SYSTEM_PROMPT = """你是 Telegram 上的动漫资源频道助手，服务中文用户。

## 你会被问到什么
- 「进击的巨人是什么 / 讲什么」→ bangumi_search 拿 id，再 bangumi_detail 取简介
- 「春物评分多少」→ bangumi_search，直接回答分数、排名、评分人数
- 「2026 年有什么穿越番推荐，评分多少」→ bangumi_browse（tags、year_from/year_to）
- 「近十年评分最高的动画」→ bangumi_browse（max_rank=100 或 min_votes）
- 「频道里发过没 / 有没有资源」→ channel_search
- 时效性信息（新番情报、新闻）→ web_search

## 硬规矩
1. **需要事实就用工具**。评分、排名、放送日期、简介这类会变或记不准的信息，不许凭记忆说。
2. **按条件找片子必须用 bangumi_browse**。bangumi_search 只能按片名匹配，
   拿它查「今年有什么番」得到的是垃圾。
3. **按评分排序时一定给 min_votes**（推荐 1000）。不给的话只有一两个人打分的
   冷门条目会排在最前面 —— 那是个 10 分的无名条目，不是「最高分动画」。
4. 工具返回空就直说「没查到」，不要编。Bangumi 是中文圈数据库，日文原名也能搜。
5. **不要写 markdown 表格**，Telegram 不渲染它，会显示成一堆竖线。用
   「- 作品名 — 评分 8.0 · 9756 人 · 7 月」这种列表。
6. 中文回答，简洁。先给结论，再给必要的细节。不要复述问题。
7. 不要输出「我会帮你查一下」这类过程话术 —— 用户已经能看到工具进度提示。
8. 不要自称 AI、不要提模型名字、不要解释自己怎么工作。
9. 用户问「发过没」时，渠道只有本 bot 索引的两个频道；找到就给原帖链接。
10. 链接只写工具返回的那个（bgm.tv/subject/... 或 t.me/...），不要自己拼。

## 当前时间
{today}（{weekday}）
"""

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(slots=True)
class ChatResult:
    text: str
    tool_calls: int = 0
    rounds: int = 0
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False     # 到工具上限被强制收尾


def system_prompt(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return SYSTEM_PROMPT.format(today=today.isoformat(), weekday=_WEEKDAYS[today.weekday()])


class ChatEngine:
    def __init__(self, settings: Settings, client: ClaudeClient, toolbox: ToolBox) -> None:
        self._cfg = settings
        self._client = client
        self._tools = toolbox

    async def answer(
        self,
        conversation: Conversation,
        question: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> ChatResult:
        """跑完一整轮问答，返回最终文本。工具过程中的中间文本会通过 on_text 推出去。"""
        conversation.add_user(question)
        history = conversation.trimmed(self._cfg.ai_session_messages)
        tools = ToolBox.specs()
        system = system_prompt()

        deadline = asyncio.get_running_loop().time() + self._cfg.ai_timeout
        # 草稿流要看「到目前为止所有轮次说过的话」，否则工具轮的前言会在下一轮
        # 开始时从屏幕上消失。但**最终消息只用最后一轮** —— 中间那几句「我查一下」
        # 是过程话术，留在正式回答里是噪音。
        streaming: list[str] = []
        last_round = None
        calls = 0
        rounds = 0

        for round_index in range(self._cfg.ai_max_tool_rounds + 1):
            if cancel is not None and cancel.is_set():
                raise Stopped
            if asyncio.get_running_loop().time() > deadline:
                raise ExternalServiceError("AI", "整轮对话超时")

            rounds += 1
            # 最后一轮：不允许再调工具，逼模型用已有信息收尾。
            force_final = round_index >= self._cfg.ai_max_tool_rounds
            round_tools = None if force_final else tools
            round_start = len(streaming)

            def collect(piece: str, _buf: list[str] = streaming) -> None:
                # 契约：on_text 收到的是「到目前为止的完整文本」，不是增量 ——
                # 上层直接把它渲染成一版草稿，不做拼接。
                _buf.append(piece)
                if on_text is not None:
                    on_text("".join(_buf))

            last_round = await self._request(history, round_tools, system, collect, cancel)

            assistant_blocks = [b for b in last_round.content if b["type"] in ("text", "tool_use")]
            if not assistant_blocks:
                # 只有 thinking：模型没产出。补一条空的 text 让协议成立。
                fallback_text = last_round.text or "（没想好怎么回答）"
                assistant_blocks = [{"type": "text", "text": fallback_text}]
            history.append({"role": "assistant", "content": assistant_blocks})
            conversation.add_assistant(assistant_blocks)

            tool_uses = [b for b in assistant_blocks if b["type"] == "tool_use"]
            if not tool_uses:
                return ChatResult(
                    text="".join(streaming[round_start:]).strip()
                    or "（模型没有返回内容）",
                    tool_calls=calls,
                    rounds=rounds,
                    stop_reason=last_round.stop_reason,
                    usage=last_round.usage,
                )

            results: list[dict[str, Any]] = []
            for block in tool_uses:
                calls += 1
                notice = TOOL_LABELS.get(block.get("name", ""), "🛠 正在调用工具")
                if on_notice is not None:
                    on_notice(f"{notice}…")
                output = await self._tools.run(block.get("name", ""), block.get("input") or {})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": output,
                    }
                )
            results_msg = {"role": "user", "content": results}
            history.append(results_msg)
            conversation.add_tool_results(results)

        # 走到这里说明最后一轮仍然要工具（不该发生，force_final 已经禁用）。
        METRICS.incr("ai.tool_limit")
        text = last_round.text.strip() if last_round and last_round.text else ""
        return ChatResult(
            text=text or "查得有点久，先给到这里。可以把问题拆小一点再问。",
            tool_calls=calls,
            rounds=rounds,
            stop_reason=last_round.stop_reason if last_round else None,
            usage=last_round.usage if last_round else {},
            truncated=True,
        )

    async def _request(
        self,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
        on_text: Callable[[str], None],
        cancel: asyncio.Event | None,
    ):
        """一轮请求，带主模型 → 兜底模型的降级。

        只在**第一个 token 之前**失败时降级：已经开始吐字了再换模型，用户会看到
        两段文风不同的回答拼在一起，比直接报错更糟。
        """
        primary = self._cfg.ai_model
        fallback = self._cfg.ai_fallback_model
        try:
            return await self._client.stream_round(
                _strip_foreign_keys(history),
                tools=tools,
                system=system,
                on_text=on_text,
                cancel=cancel,
                model=primary,
            )
        except ExternalServiceError as exc:
            if not fallback or fallback == primary:
                raise
            METRICS.incr("ai.fallback", model=primary)
            log.warning("ai.fallback", from_model=primary, to_model=fallback,
                        error=str(exc)[:160])
            return await self._client.stream_round(
                _strip_foreign_keys(history),
                tools=tools,
                system=system,
                on_text=on_text,
                cancel=cancel,
                model=fallback,
            )


def _strip_foreign_keys(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留协议认识的字段。

    回灌 history 时不能带 `cache_control` / `citations` 这类上游加的字段 ——
    网关对未知字段会 400（实测 thinking 块就是这样炸的）。
    """
    clean: list[dict[str, Any]] = []
    for msg in history:
        blocks = []
        for block in msg.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif kind == "tool_use":
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input") or {},
                    }
                )
            elif kind == "tool_result":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": block.get("content", ""),
                    }
                )
        if blocks:
            clean.append({"role": msg.get("role", "user"), "content": blocks})
    return clean
