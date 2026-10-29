"""AI 对话：SSE 解析、工具循环、流式草稿、Markdown 转换、会话。

不连 Telegram、不连真模型。最该盯的三处：

1. **thinking 块不能回流**。网关对回传的 thinking 直接 400，而模型每轮都会产出它。
2. **工具轮数必须有上限**，且到上限时要逼模型收尾，不能截断成半个答案。
3. **草稿是临时消息**，最终必须再发一条完整消息，否则用户 30 秒后什么都看不到。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from animebot.config import Settings
from animebot.core.errors import ExternalServiceError
from animebot.features.ai import handlers as ai_handlers
from animebot.features.ai.chat import ChatEngine, _strip_foreign_keys, system_prompt
from animebot.features.ai.client import ClaudeClient, Round, iter_sse, sse_lines
from animebot.features.ai.render import to_telegram_html
from animebot.features.ai.session import ConversationStore
from animebot.features.ai.stream import DraftStreamer, split_text
from animebot.features.ai.tools import ToolBox, parse_bing, unwrap_bing

USER_ID = 8100
CHAT_ID = USER_ID
NOW = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=dt.UTC)

SSE_SAMPLE = """event: message_start
data: {"type":"message_start","message":{"role":"assistant"}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"要查"}}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"8.2"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":" 分"}}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}

event: message_stop
data: {"type":"message_stop"}

"""

def _sse(event: str, payload: dict[str, Any]) -> str:
    """一帧 SSE。拼长 JSON 字面量必然超行宽，用函数构造更清楚。"""
    return "event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _delta(index: int, partial: str) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial},
    }


SSE_TOOL = "".join([
    _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": ""}}),
    _sse("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "tool_use", "id": "call_1",
                                                   "name": "bangumi_search", "input": {}}}),
    _sse("content_block_delta", _delta(1, '{"keyword":')),
    _sse("content_block_delta", _delta(1, '"进击"}')),
    _sse("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "tool_use"}}),
])


# ---------------------------------------------------------------- SSE


async def test_iter_sse_parses_events() -> None:
    events = [ev async for ev in iter_sse(sse_lines([SSE_SAMPLE]))]
    assert [e["type"] for e in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "message_delta",
        "message_stop",
    ]


async def test_iter_sse_handles_split_chunks_and_no_trailing_blank() -> None:
    """真实网络里一帧可能被切成两段，最后也可能没有空行收尾。"""
    parts = SSE_SAMPLE.split("\n\n")
    chunks = [parts[0] + "\n", "\n".join(parts[1:4]) + "\n\n", "\n\n".join(parts[4:])]
    events = [ev async for ev in iter_sse(sse_lines(chunks))]
    assert any(e["type"] == "message_delta" for e in events)


async def test_iter_sse_skips_garbage() -> None:
    stream = 'data: {"type":"message_stop"}\n\ndata: not-json\n\ndata: [DONE]\n\n'
    events = [ev async for ev in iter_sse(sse_lines([stream]))]
    assert [e["type"] for e in events] == ["message_stop"]


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    @property
    def content(self) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            for line in self._body.splitlines(keepends=True):
                yield line.encode()

        return gen()

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeSession:
    closed = False

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.bodies: list[dict[str, Any]] = []

    def post(self, _url: str, *, json: dict[str, Any], **_kw: Any) -> FakeResponse:
        self.bodies.append(json)
        return self._response


@pytest.fixture
def ai_settings(tmp_path) -> Settings:
    return Settings(
        bot_token="test:token",
        ai_api_key="sk-test",
        ai_model="primary-model",
        ai_fallback_model="fallback-model",
        ai_max_tokens=300,
        ai_max_tool_rounds=2,
        ai_session_ttl=60.0,
        db_path=tmp_path / "t.db",
        log_dir=tmp_path / "logs",
    )


async def test_stream_round_extracts_text_and_drops_thinking(ai_settings: Settings) -> None:
    client = ClaudeClient(ai_settings, session=FakeSession(FakeResponse(SSE_SAMPLE)))  # type: ignore[arg-type]
    seen: list[str] = []
    round_ = await client.stream_round(
        [{"role": "user", "content": [{"type": "text", "text": "评分?"}]}],
        on_text=seen.append,
    )
    assert round_.text == "8.2 分"
    assert round_.stop_reason == "end_turn"
    assert [b["type"] for b in round_.content] == ["text"], "thinking 不能回流，网关会 400"
    assert "".join(seen) == "8.2 分", "client 的 on_text 是增量，累计由 engine 做"


async def test_stream_round_reassembles_tool_input(ai_settings: Settings) -> None:
    client = ClaudeClient(ai_settings, session=FakeSession(FakeResponse(SSE_TOOL)))  # type: ignore[arg-type]
    round_ = await client.stream_round(
        [{"role": "user", "content": [{"type": "text", "text": "查"}]}]
    )
    assert round_.stop_reason == "tool_use"
    tool = next(b for b in round_.content if b["type"] == "tool_use")
    assert tool["input"] == {"keyword": "进击"}, "分片的 input_json_delta 要拼成完整对象"


async def test_stream_round_http_error_becomes_external_error(ai_settings: Settings) -> None:
    client = ClaudeClient(
        ai_settings, session=FakeSession(FakeResponse("boom", status=500))  # type: ignore[arg-type]
    )
    with pytest.raises(ExternalServiceError):
        await client.stream_round([{"role": "user", "content": []}])


# ---------------------------------------------------------------- 工具循环


class FakeClient:
    """按脚本返回若干轮结果。`calls` 记录每次请求的 messages 快照。"""

    def __init__(self, rounds: list[Round], fail_first: bool = False) -> None:
        self._rounds = rounds
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.fail_first = fail_first

    async def stream_round(self, messages, *, tools=None, system=None, on_text=None,
                           on_thinking=None, cancel=None, model=None):
        if self.fail_first and not self.calls:
            self.calls.append((model, [dict(m) for m in messages]))
            raise ExternalServiceError("AI", "primary down")
        self.calls.append((model, [dict(m) for m in messages]))
        round_ = self._rounds.pop(0)
        if on_text is not None:
            on_text(round_.text)
        return round_


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return '{"score": 8.2}'


def tool_round(*, tool_id: str = "call_1", name: str = "bangumi_search") -> Round:
    return Round(
        content=[
            {"type": "text", "text": "我查一下。"},
            {"type": "tool_use", "id": tool_id, "name": name, "input": {"keyword": "进击"}},
        ],
        text="我查一下。",
        stop_reason="tool_use",
    )


def text_round(text: str = "8.2 分") -> Round:
    return Round(content=[{"type": "text", "text": text}], text=text, stop_reason="end_turn")


async def test_tool_loop_feeds_results_back(ai_settings: Settings) -> None:
    client = FakeClient([tool_round(), text_round()])
    tools = FakeTools()
    engine = ChatEngine(ai_settings, client, tools)  # type: ignore[arg-type]
    store = ConversationStore()

    notices: list[str] = []
    result = await engine.answer(
        store.get((1, 1)), "评分?", on_notice=notices.append
    )

    assert result.text == "8.2 分"
    assert result.tool_calls == 1
    assert tools.calls == [("bangumi_search", {"keyword": "进击"})]
    assert notices and "Bangumi" in notices[0]

    second = client.calls[1][1]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "call_1"


async def test_streaming_includes_preamble_but_final_answer_does_not(
    ai_settings: Settings,
) -> None:
    """草稿要连续（工具轮的前言也在），最终消息只留最后一轮 —— 否则「我查一下」
    这种过程话术会混进正式回答。"""
    client = FakeClient([tool_round(), text_round("8.2 分")])
    engine = ChatEngine(ai_settings, client, FakeTools())  # type: ignore[arg-type]

    seen: list[str] = []
    result = await engine.answer(
        ConversationStore().get((1, 1)), "评分?", on_text=seen.append
    )

    assert result.text == "8.2 分"
    assert seen[-1] == "我查一下。8.2 分", "草稿期间保留前言，避免文字突然消失"


async def test_tool_rounds_capped_and_forced_to_finish(ai_settings: Settings) -> None:
    """上限 2 轮工具。第三轮必须不带 tools，逼模型收尾。"""
    client = FakeClient([
        tool_round(tool_id="a"),
        tool_round(tool_id="b", name="web_search"),
        text_round("收尾了"),
    ])
    engine = ChatEngine(ai_settings, client, FakeTools())  # type: ignore[arg-type]

    result = await engine.answer(ConversationStore().get((1, 1)), "查")

    assert result.text == "收尾了"
    assert result.tool_calls == 2
    assert result.rounds == 3


async def test_fallback_model_used_on_first_round_failure(ai_settings: Settings) -> None:
    client = FakeClient([text_round("兜底回答")], fail_first=True)
    engine = ChatEngine(ai_settings, client, FakeTools())  # type: ignore[arg-type]

    result = await engine.answer(ConversationStore().get((1, 1)), "问")

    assert result.text == "兜底回答"
    assert [c[0] for c in client.calls] == ["primary-model", "fallback-model"]


def test_strip_foreign_keys_keeps_protocol_shape() -> None:
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"x": 1}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t",
                                           "input": {}, "citations": []}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1",
                                      "content": "ok", "is_error": True}]},
    ]
    clean = _strip_foreign_keys(history)
    assert clean[0]["content"][0] == {"type": "text", "text": "hi"}
    assert "citations" not in clean[1]["content"][0]
    assert "is_error" not in clean[2]["content"][0]


def test_system_prompt_has_date() -> None:
    prompt = system_prompt(dt.date(2026, 10, 28))
    assert "2026-10-28" in prompt and "周三" in prompt


# ---------------------------------------------------------------- 会话


def test_conversation_trims_from_text_user_turn() -> None:
    store = ConversationStore()
    conv = store.get((1, 1))
    conv.add_user("q1")
    conv.add_assistant([{"type": "text", "text": "a1"}])
    conv.add_user("q2")
    conv.add_assistant([{"type": "tool_use", "id": "t", "name": "x", "input": {}}])
    conv.add_tool_results([{"type": "tool_result", "tool_use_id": "t", "content": "r"}])

    trimmed = conv.trimmed(4)
    assert trimmed[0]["role"] == "user"
    assert trimmed[0]["content"][0] == {"type": "text", "text": "q2"}, (
        "不能从 tool_result 处截断 —— 那会让协议不成立"
    )
    assert [m["role"] for m in trimmed] == ["user", "assistant", "user"]


def test_conversation_store_reset_and_ttl() -> None:
    store = ConversationStore(ttl=0.0)
    store.get((1, 1)).add_user("q")
    assert store.reset((1, 1)) is True
    assert store.reset((1, 1)) is False
    # ttl=0 时下一次取就是新的会话
    assert store.get((2, 2)).messages == []


# ---------------------------------------------------------------- 渲染 / 分片


def test_markdown_to_telegram_html() -> None:
    out = to_telegram_html("## 结论\n**8.2 分**，看 [原帖](https://t.me/x/1)\n- 第一季\n`code`")
    assert "<b>结论</b>" in out
    assert "<b>8.2 分</b>" in out
    assert '<a href="https://t.me/x/1">原帖</a>' in out
    assert "· 第一季" in out
    assert "<code>code</code>" in out


def test_markdown_escapes_html_and_drops_unsafe_links() -> None:
    out = to_telegram_html("a < b & c\n[点我](javascript:alert(1))")
    assert "a &lt; b &amp; c" in out
    assert "<a href" not in out, "javascript: 链接不能变成可点的 <a>"
    assert "点我" in out


def test_code_block_is_not_interpreted() -> None:
    out = to_telegram_html("```python\nprint('**not bold**')\n```")
    assert "<pre>print('**not bold**')</pre>" in out
    assert "<b>" not in out


def test_split_text_prefers_paragraph_boundary() -> None:
    text = "a" * 60 + "\n\n" + "b" * 60
    chunks = split_text(text, limit=80)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 60


def test_split_text_hard_cuts_when_no_break() -> None:
    chunks = split_text("x" * 200, limit=80)
    assert all(len(c) <= 80 for c in chunks)
    assert "".join(chunks) == "x" * 200


def test_split_text_empty_returns_placeholder() -> None:
    assert split_text("   ") == ["（模型没有返回内容）"]


# ---------------------------------------------------------------- 工具解析


def test_unwrap_bing_decodes_redirect() -> None:
    import base64

    target = "https://bgm.tv/subject/1"
    token = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    url = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{token}&ntb=1"
    assert unwrap_bing(url) == target
    assert unwrap_bing("https://example.com/x") == "https://example.com/x"


def test_parse_bing_extracts_results() -> None:
    page = (
        '<ol><li class="b_algo"><h2><a href="https://a.example">标题一</a></h2>'
        "<p>摘要一</p></li>"
        '<li class="b_algo"><h2><a href="https://b.example">标题二</a></h2></li></ol>'
    )
    results = parse_bing(page)
    assert [r["title"] for r in results] == ["标题一", "标题二"]
    assert results[0]["snippet"] == "摘要一"


def test_parse_bing_returns_empty_on_unknown_layout() -> None:
    assert parse_bing("<html>人机验证</html>") == []


async def test_toolbox_reports_errors_as_payload(ai_settings: Settings) -> None:
    class Boom(ToolBox):
        async def web_search(self, query: str) -> dict[str, Any]:
            raise RuntimeError("网络挂了")

    box = Boom(ai_settings)
    payload = await box.run("web_search", {"query": "x"})
    assert "RuntimeError" in payload and "网络挂了" in payload


async def test_toolbox_unknown_tool(ai_settings: Settings) -> None:
    box = ToolBox(ai_settings)
    assert "没有这个工具" in await box.run("nope", {})


# ---------------------------------------------------------------- 草稿流


class DraftRecorder:
    def __init__(self) -> None:
        self.drafts: list[tuple[int, int, str]] = []

    async def send_message_draft(self, *, chat_id: int, draft_id: int, text: str) -> bool:
        self.drafts.append((chat_id, draft_id, text))
        return True


async def test_draft_streamer_pushes_latest_text() -> None:
    rec = DraftRecorder()
    streamer = DraftStreamer(
        bot=rec,  # type: ignore[arg-type]
        chat_id=1,
        draft_id=9,
        render=lambda t: t,
        interval=0.0,
    )
    streamer.on_text("评分")
    task = asyncio.create_task(streamer.pump())
    await asyncio.sleep(0.05)
    streamer.on_text("评分 8.2")
    await asyncio.sleep(0.05)
    streamer.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    texts = [d[2] for d in rec.drafts]
    assert texts[0] == "评分"
    assert "8.2" in texts[-1]


async def test_draft_streamer_gives_up_on_api_error() -> None:
    from aiogram.exceptions import TelegramBadRequest

    class Failing:
        async def send_message_draft(self, **_kw: Any) -> bool:
            raise TelegramBadRequest(method=None, message="Bad Request: not supported")  # type: ignore[arg-type]

    streamer = DraftStreamer(bot=Failing(), chat_id=1, draft_id=9, render=lambda t: t)  # type: ignore[arg-type]
    streamer.on_text("x")
    await streamer.pump()      # 不该抛
    assert streamer.supported is False


# ---------------------------------------------------------------- 指令装配


def msg_update(text: str, *, chat_type: str = "private") -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=CHAT_ID if chat_type == "private" else -100123,
                      type=chat_type),
            from_user=User(id=USER_ID, is_bot=False, first_name="T"),
            text=text,
        ),
    )


@pytest.fixture
async def fake_bot() -> AsyncIterator[Bot]:
    bot = Bot(token="42:TEST-TOKEN-NOT-REAL")
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def dp(ai_settings: Settings) -> Dispatcher:
    """带上 Trace + ErrorBoundary —— 用法错误要变成一条回复，而不是抛给调用方。"""
    from animebot.bot.middlewares import ErrorBoundary, TraceMiddleware
    from animebot.bot.wiring import bind_commands, collect_specs
    from animebot.core.registry import CommandRegistry

    d = Dispatcher()
    d["settings"] = ai_settings
    d["engine"] = ChatEngine(ai_settings, FakeClient([text_round()]), FakeTools())  # type: ignore[arg-type]
    d["conversations"] = ConversationStore()
    reg = CommandRegistry()
    d["registry"] = reg
    for mw in (TraceMiddleware(reg), ErrorBoundary()):
        d.update.outer_middleware(mw)

    router = Router(name="ai")
    bind_commands(router, collect_specs(ai_handlers), reg, "ai")
    ai_handlers.register_callbacks(router)
    d.include_router(router)
    return d


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    async def fake_reply(self: Message, text: str, **_kw: Any) -> None:
        out.append(text)

    async def fake_answer(self: Message, text: str, **_kw: Any) -> None:
        out.append(text)

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    monkeypatch.setattr(Message, "answer", fake_answer, raising=True)
    return out


async def test_q_without_key_tells_user(
    ai_settings: Settings, fake_bot: Bot, sent: list[str], dp: Dispatcher
) -> None:
    from pydantic import SecretStr

    dp["settings"] = ai_settings.model_copy(update={"ai_api_key": SecretStr("")})
    await dp.feed_update(fake_bot, msg_update("/q 评分多少"))
    assert sent and "还没配好" in sent[0]


async def test_q_without_argument_shows_usage(
    ai_settings: Settings, fake_bot: Bot, sent: list[str], dp: Dispatcher
) -> None:
    await dp.feed_update(fake_bot, msg_update("/q"))
    assert sent and "用法" in sent[0]


async def test_q_reset_clears_conversation(
    ai_settings: Settings, fake_bot: Bot, sent: list[str], dp: Dispatcher
) -> None:
    store: ConversationStore = dp["conversations"]
    store.get((CHAT_ID, USER_ID)).add_user("旧问题")
    await dp.feed_update(fake_bot, msg_update("/q 重置"))
    assert store.reset((CHAT_ID, USER_ID)) is False, "reset 之后应该已经没了"
    assert "已开新对话" in sent[0]


async def test_q_answers_and_sends_final_message(
    ai_settings: Settings, fake_bot: Bot, sent: list[str], dp: Dispatcher
) -> None:
    await dp.feed_update(fake_bot, msg_update("/q 评分多少"))
    assert sent == ["8.2 分"], "最终必须有一条完整消息（草稿是临时的）"
