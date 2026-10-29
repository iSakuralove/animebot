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
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from animebot.bot.ui import Sent
from animebot.config import Settings
from animebot.core.errors import ExternalServiceError
from animebot.features.ai import handlers as ai_handlers
from animebot.features.ai.chat import ChatEngine, _strip_foreign_keys, system_prompt
from animebot.features.ai.client import ClaudeClient, Round, iter_sse, sse_lines
from animebot.features.ai.render import to_telegram_html
from animebot.features.ai.session import ConversationStore
from animebot.features.ai.stream import EditStreamer, split_text
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


# ---------------------------------------------------------------- Bangumi 工具


class CapturingBox(ToolBox):
    """不发网络请求，只记录构造出来的请求体。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.posts: list[tuple[dict[str, Any], int]] = []
        self.gets: list[str] = []

    async def _bgm_post(self, body: dict[str, Any], *, limit: int) -> dict[str, Any]:
        self.posts.append((body, limit))
        return {"data": [{"id": 1, "name": "進撃の巨人", "name_cn": "进击的巨人",
                          "date": "2013-04-06",
                          "rating": {"score": 8.2, "rank": 114, "total": 32119}}],
                "total": 32}

    async def _bgm_get(self, url: str) -> dict[str, Any]:
        self.gets.append(url)
        return {
            "id": 55770, "name": "進撃の巨人", "name_cn": "进击的巨人",
            "platform": "TV", "date": "2013-04-06", "total_episodes": 25,
            "summary": "人类被巨人捕食。",
            "tags": [{"name": "热血", "count": 5997}],
            "infobox": [
                {"key": "放送开始", "value": "2013年4月6日"},
                {"key": "导演", "value": "荒木哲郎"},
                {"key": "别名", "value": [{"v": "Attack on Titan"}, {"v": "AoT"}]},
                {"key": "价格", "value": "1000 日元"},
            ],
            "rating": {"score": 8.2, "rank": 114, "total": 32119,
                       "count": {"10": 4551, "9": 8032}},
            "collection": {"wish": 100, "collect": 200, "doing": 30, "dropped": 5},
        }


async def test_bangumi_search_shape(ai_settings: Settings) -> None:
    box = CapturingBox(ai_settings)
    payload = json.loads(await box.run("bangumi_search", {"keyword": "进击的巨人"}))

    body, limit = box.posts[0]
    assert body["filter"]["type"] == [2]
    assert body["filter"]["nsfw"] is False, "默认排除 R18"
    assert limit == 10
    assert payload["total"] == 32
    row = payload["results"][0]
    assert (row["score"], row["rank"], row["votes"]) == (8.2, 114, 32119)
    assert row["url"] == "https://bgm.tv/subject/1"


async def test_bangumi_detail_keeps_summary_and_staff(ai_settings: Settings) -> None:
    box = CapturingBox(ai_settings)
    row = json.loads(await box.run("bangumi_detail", {"subject_id": 55770}))

    assert box.gets == ["https://api.bgm.tv/v0/subjects/55770"]
    assert row["episodes"] == 25
    assert row["platform"] == "TV"
    assert "人类被巨人捕食" in row["summary"]
    assert row["staff"]["导演"] == "荒木哲郎"
    assert row["staff"]["别名"] == "Attack on Titan、AoT", "列表型 infobox 要拼成一句"
    assert "价格" not in row["staff"], "无关字段不该占 token"
    assert row["rating_dist"]["10"] == 4551
    assert row["collection"]["collect"] == 200


async def test_bangumi_detail_rejects_bad_id(ai_settings: Settings) -> None:
    box = CapturingBox(ai_settings)
    out = await box.run("bangumi_detail", {"subject_id": "进击的巨人"})
    assert "subject_id 必须是整数" in out
    assert box.gets == []


async def test_bangumi_browse_builds_filters(ai_settings: Settings) -> None:
    box = CapturingBox(ai_settings)
    await box.run("bangumi_browse", {
        "tags": ["穿越"], "year_from": 2026, "year_to": 2026,
        "min_votes": 500, "sort": "score", "limit": 5,
    })
    body, limit = box.posts[0]
    f = body["filter"]
    assert body["keyword"] == "", "按条件浏览时 keyword 是空串"
    assert body["sort"] == "score"
    assert f["tag"] == ["穿越"]
    assert f["air_date"] == [">=2026-01-01", "<2027-01-01"]
    assert f["rating_count"] == [">=500"]
    assert limit == 5


async def test_bangumi_browse_forces_vote_floor_for_score_sort(
    ai_settings: Settings,
) -> None:
    """按评分排序却不设人数下限，会把 1 人打 10 分的冷门条目排第一。"""
    box = CapturingBox(ai_settings)
    await box.run("bangumi_browse", {"sort": "score"})
    assert box.posts[0][0]["filter"]["rating_count"] == [">=300"]


async def test_bangumi_browse_excludes_unranked(ai_settings: Settings) -> None:
    """rank=0 是「还没排上名」。sort=rank 时它们会被排在最前，必须排除。"""
    box = CapturingBox(ai_settings)
    await box.run("bangumi_browse", {"max_rank": 100, "sort": "rank"})
    assert box.posts[0][0]["filter"]["rank"] == [">=1", "<=100"]


async def test_bangumi_browse_caps_limit(ai_settings: Settings) -> None:
    box = CapturingBox(ai_settings)
    await box.run("bangumi_browse", {"limit": 999})
    assert box.posts[0][1] == 20, "服务端硬顶 20"


async def test_bangumi_token_goes_in_header(ai_settings: Settings) -> None:
    from pydantic import SecretStr

    cfg = ai_settings.model_copy(update={"bgm_token": SecretStr("tok-123")})
    box = ToolBox(cfg)
    assert box._bgm_headers()["Authorization"] == "Bearer tok-123"
    assert "Authorization" not in ToolBox(ai_settings)._bgm_headers(), "没配就不带"


# ---------------------------------------------------------------- 流式编辑


class EditRecorder:
    """记录 edit_message_text / send_message，不真发请求。"""

    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []
        self.sent: list[str] = []
        self.fail_edit: Exception | None = None

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str,
                                **_kw: Any) -> bool:
        if self.fail_edit is not None:
            raise self.fail_edit
        self.edits.append((message_id, text))
        return True

    async def send_message(self, *, chat_id: int, text: str, **_kw: Any) -> bool:
        self.sent.append(text)
        return True


def make_streamer(rec: EditRecorder, **kw: Any) -> EditStreamer:
    async def send_extra(html: str) -> None:
        rec.sent.append(html)

    return EditStreamer(
        bot=rec,  # type: ignore[arg-type]
        sent=Sent(chat_id=1, message_id=77),
        render=lambda t: t,
        send_extra=send_extra,
        **kw,
    )


async def test_streamer_edits_one_message_in_place() -> None:
    """一条消息从头长到尾 —— 不能每版都发新消息（那正是「回了两遍」的来源）。"""
    rec = EditRecorder()
    streamer = make_streamer(rec, interval=0.0)
    streamer.on_text("评分")
    task = asyncio.create_task(streamer.pump())
    await asyncio.sleep(0.05)
    streamer.on_text("评分 8.2")
    await asyncio.sleep(0.05)
    streamer.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rec.sent == [], "过程中一条新消息都不该发"
    assert [m for m, _ in rec.edits] == [77, 77], "始终编辑同一条"
    assert "8.2" in rec.edits[-1][1]


async def test_streamer_finish_edits_placeholder_and_sends_overflow() -> None:
    rec = EditRecorder()
    streamer = make_streamer(rec)
    long_text = "a" * 4000 + "\n\n" + "b" * 4000
    await streamer.finish(long_text)

    assert rec.edits, "最终文本要写进原来那条占位消息"
    assert len(rec.sent) == len(split_text(long_text)) - 1, "只有超出部分另起消息"


async def test_streamer_throttle_error_does_not_kill_output() -> None:
    from aiogram.exceptions import TelegramRetryAfter

    rec = EditRecorder()
    rec.fail_edit = TelegramRetryAfter(
        method=None, message="flood", retry_after=0  # type: ignore[arg-type]
    )
    streamer = make_streamer(rec, interval=0.0)
    streamer.on_text("x")
    await streamer._flush()
    assert streamer.interval > 0, "撞限流后要放慢节奏"
    assert not streamer._broken, "限流是可恢复的，不该放弃后续输出"


async def test_streamer_gives_up_on_bad_edit() -> None:
    from aiogram.exceptions import TelegramBadRequest

    rec = EditRecorder()
    rec.fail_edit = TelegramBadRequest(
        method=None,  # type: ignore[arg-type]
        message="Bad Request: message to edit not found",
    )
    streamer = make_streamer(rec, interval=0.0)
    streamer.on_text("x")
    await streamer._flush()
    assert streamer._broken is True


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


class Outbox:
    """记录发出的消息、编辑、草稿。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.drafts: list[tuple[int, str]] = []

    def __eq__(self, other: object) -> bool:
        return isinstance(other, list) and self.sent == other

    def __iter__(self):
        return iter(self.sent)

    def __len__(self) -> int:
        return len(self.sent)

    def __getitem__(self, i):
        return self.sent[i]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    out = Outbox()

    async def fake_reply(self: Message, text: str, **_kw: Any) -> Message:
        out.sent.append(text)
        return self

    async def fake_edit(self: Bot, *, chat_id: int, message_id: int, text: str,
                        **_kw: Any) -> bool:
        out.edits.append(text)
        return True

    async def fake_draft(self: Bot, *, chat_id: int, draft_id: int, text: str,
                         **_kw: Any) -> bool:
        out.drafts.append((draft_id, text))
        return True

    async def fake_delete(self: Bot, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(Message, "reply", fake_reply, raising=True)
    monkeypatch.setattr(Message, "answer", fake_reply, raising=True)
    monkeypatch.setattr(Bot, "edit_message_text", fake_edit, raising=True)
    monkeypatch.setattr(Bot, "delete_message", fake_delete, raising=True)
    monkeypatch.setattr(Bot, "send_message_draft", fake_draft, raising=True)
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


async def test_private_uses_draft_then_one_final_message(
    ai_settings: Settings, fake_bot: Bot, sent: Outbox, dp: Dispatcher
) -> None:
    """私聊默认走草稿流：过程中只推草稿，结束发**一条**正式消息。

    草稿是临时的（30 秒消失），必须补一条正式消息；但只补一条 —— 第一次
    上线的 bug 就是草稿和正式消息同时在，看起来像回了两遍。
    """
    await dp.feed_update(fake_bot, msg_update("/q 评分多少"))
    assert sent.drafts, "私聊应该走草稿流（客户端会把它做成动画）"
    assert sent.sent == ["8.2 分"], "最后只发一条正式消息"
    assert sent.edits == [], "草稿模式不编辑消息"


def test_markdown_table_flattened_for_telegram() -> None:
    """Telegram 不渲染表格。原样发出去就是一堆竖线，所以先压成列表行。"""
    md = (
        "| 作品 | 评分 |\n"
        "|---|---|\n"
        "| 无职转生 第三季 | **8.0** |\n"
        "| 芙莉莲 第二季 | 7.5 |\n"
    )
    out = to_telegram_html(md)
    assert "|---|" not in out
    assert "<b>8.0</b>" in out
    assert out.count("· ") == 3
    assert "无职转生 第三季 | 8.0" in out.replace("<b>", "").replace("</b>", "")


async def test_html_failure_edits_same_message_instead_of_resending() -> None:
    """HTML 解析失败要退回纯文本**编辑同一条消息**，不能另发 —— 另发又是两条。"""
    from aiogram.exceptions import TelegramBadRequest

    rec = EditRecorder()

    async def send_extra(html: str, **_kw: Any) -> None:
        rec.sent.append(html)

    streamer = EditStreamer(
        bot=rec,  # type: ignore[arg-type]
        sent=Sent(chat_id=1, message_id=77),
        render=lambda t: f"<b>{t}",   # 故意产出非法 HTML 交给 Telegram 去拒
        send_extra=send_extra,
    )
    rec.fail_edit = TelegramBadRequest(
        method=None,  # type: ignore[arg-type]
        message="Bad Request: can't parse entities",
    )
    with pytest.raises(TelegramBadRequest):
        await streamer.finish("答案")
    rec.fail_edit = None
    await streamer.finish_plain("答案")
    assert rec.edits == [(77, "答案")], "纯文本也写回同一条消息"


async def test_stop_button_cancels_running_generation(
    fake_bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止按钮的 callback_data 能找回取消信号 —— 否则点了没反应。"""
    from animebot.features.ai import handlers as ai_handlers

    answered: list[str] = []

    async def fake_answer(self: CallbackQuery, text: str | None = None, **_kw: Any) -> bool:
        answered.append(text or "")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer, raising=True)

    gid, cancel = ai_handlers._new_generation()
    cb = CallbackQuery(
        id="cb-stop",
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        chat_instance="ci",
        data=f"qs:{gid}",
        message=Message(message_id=1, date=NOW,
                        chat=Chat(id=CHAT_ID, type="private"), text="🤔 正在思考…"),
    ).as_(fake_bot)
    await ai_handlers.on_stop(cb)
    assert cancel.is_set(), "取消信号必须被置位"
    assert answered == ["正在停止…"], "点一下要立刻有反馈，否则用户以为没点上"


async def test_thinking_progress_is_reported(ai_settings: Settings) -> None:
    """模型强制 thinking，期间没有任何 text_delta。不报进度用户会以为卡死。"""
    class SlowClient(FakeClient):
        async def stream_round(self, messages, *, tools=None, system=None,
                               on_text=None, on_thinking=None, cancel=None, model=None):
            if on_thinking is not None:
                for _ in range(5):
                    on_thinking("想" * 200)      # 累计 1000 字
            return await super().stream_round(
                messages, tools=tools, system=system, on_text=on_text,
                on_thinking=on_thinking, cancel=cancel, model=model,
            )

    engine = ChatEngine(ai_settings, SlowClient([text_round("答")]), FakeTools())  # type: ignore[arg-type]
    notices: list[str] = []
    await engine.answer(
        ConversationStore().get((1, 1)), "问", on_notice=notices.append
    )
    assert any("正在思考" in n for n in notices), f"要报思考进度，实际 {notices}"
    assert any("字" in n for n in notices)


async def test_reveal_paces_bursty_text_into_multiple_frames() -> None:
    """网关把整段正文一次性推过来（实测 0.7s 出 130 个 delta）。

    不控制回放的话，用户 0.7 秒里看不到任何中间态 —— 观感就是「没有流式」。
    """
    rec = EditRecorder()
    streamer = make_streamer(rec, interval=0.0)
    body = "".join(f"第{i}句。" for i in range(100))      # 400 字
    streamer.on_text(body)

    task = asyncio.create_task(streamer.pump())
    await asyncio.sleep(0.05)
    streamer.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    lengths = [len(text) for _, text in rec.edits]
    assert len(lengths) >= 4, f"应该分多版长出来，实际 {lengths}"
    assert lengths == sorted(lengths), "回放进度不能倒退"
    assert lengths[0] < len(body) * 0.8, "第一版不能一次蹦完全文"
    assert lengths[0] >= 24, "第一版至少给几个字，别一上来只有一个字"


async def test_reveal_finishes_immediately_on_finish() -> None:
    """收尾必须准：finish 之后屏幕上就是完整文本，不能卡在回放中途。"""
    rec = EditRecorder()
    streamer = make_streamer(rec, interval=999.0)   # 泵几乎不跑
    body = "x" * 400
    streamer.on_text(body)
    await streamer.finish(body)
    assert rec.edits and rec.edits[-1][1] == body


async def test_notice_shown_immediately_without_reveal() -> None:
    """工具提示不该被回放拖慢 —— 它本来就是「现在在干什么」。"""
    rec = EditRecorder()
    streamer = make_streamer(rec, interval=0.0)
    streamer.set_notice("📊 正在翻 Bangumi 榜单…")
    await streamer._flush()
    assert rec.edits
    assert rec.edits[-1][1] == "📊 正在翻 Bangumi 榜单…"


async def test_notice_yields_to_streaming_text() -> None:
    """正文开始流之后，提示要让位。

    两件事：提示不能被正文永久压在下面（用户看不到「在干什么」），
    正文开始后也不能被新提示抢屏（那会把正在读的答案换成一句话）。
    """
    rec = EditRecorder()
    streamer = make_streamer(rec, interval=0.0)
    streamer.set_notice("🤔 正在思考…")
    streamer.on_text("答案开头")
    await streamer._flush()
    assert rec.edits[-1][1] == "答案开头", "正文一出现就撤掉提示行"

    streamer.set_notice("🌐 正在联网搜索…")
    await streamer._flush()
    assert "正在联网搜索" not in rec.edits[-1][1], "正文在流时新提示不抢屏"


async def test_group_uses_edit_not_draft(
    ai_settings: Settings, fake_bot: Bot, sent: Outbox, dp: Dispatcher
) -> None:
    """草稿不支持群聊，所以群里退回占位消息 + 编辑。"""
    update = Update(update_id=9, message=Message(
        message_id=9, date=NOW,
        chat=Chat(id=-1001234567890, type="supergroup"),
        from_user=User(id=USER_ID, is_bot=False, first_name="T"),
        text="/q 评分多少",
    ))
    await dp.feed_update(fake_bot, update)
    assert sent.drafts == [], "群里发不了草稿"
    assert sent.sent and sent.sent[0] == "🤔 正在思考…", "先发占位消息"
    assert sent.edits and sent.edits[-1] == "8.2 分", "答案编辑进同一条"


async def test_edit_mode_setting_forces_edit_in_private(
    ai_settings: Settings, fake_bot: Bot, sent: Outbox
) -> None:
    """`ai_stream_mode=edit` 时私聊也用编辑（想少一条正式消息时用）。"""
    from aiogram import Dispatcher as _D
    from aiogram import Router as _R

    from animebot.bot.middlewares import ErrorBoundary, TraceMiddleware
    from animebot.bot.wiring import bind_commands, collect_specs
    from animebot.core.registry import CommandRegistry

    cfg = ai_settings.model_copy(update={"ai_stream_mode": "edit"})
    d = _D()
    d["settings"] = cfg
    d["engine"] = ChatEngine(cfg, FakeClient([text_round()]), FakeTools())  # type: ignore[arg-type]
    d["conversations"] = ConversationStore()
    reg = CommandRegistry()
    d["registry"] = reg
    for mw in (TraceMiddleware(reg), ErrorBoundary()):
        d.update.outer_middleware(mw)
    router = _R(name="ai")
    bind_commands(router, collect_specs(ai_handlers), reg, "ai")
    ai_handlers.register_callbacks(router)
    d.include_router(router)

    await d.feed_update(fake_bot, msg_update("/q 评分多少"))
    assert sent.drafts == []
    assert sent.sent[0] == "🤔 正在思考…"
    assert sent.edits[-1] == "8.2 分"


async def test_draft_stop_event_cancels_generation(fake_bot: Bot) -> None:
    """草稿模式下停止按钮由 Telegram 自带，回传的更新里只有 draft_id。"""
    from aiogram.types import Chat as _C
    from aiogram.types import MessageGenerationStopped

    gid, cancel = ai_handlers._new_generation()
    draft_id = ai_handlers._new_draft_id(gid)
    event = MessageGenerationStopped(
        chat=_C(id=USER_ID, type="private"), draft_id=draft_id
    )
    await ai_handlers.on_generation_stopped(event)
    assert cancel.is_set()
