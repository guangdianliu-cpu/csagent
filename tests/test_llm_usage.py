"""M4.1：从 OpenAI 兼容响应解析 token 用量，并记进审计缓冲。

主张三条：
1. 缺字段、类型不对、流式中间片没有 usage → ``(0, 0)``，不能把调用方带崩。
2. 流式末片给的是累计值：非零覆盖，不在客户端把增量加起来。
3. 用量进审计缓冲与 ``audit_log_enabled`` 无关；JSONL 由 fill_from_buffer 抄过去。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from csagent.audit.session_logger import SessionAuditLogger, SessionAuditRecord
from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.infra.llm_client import (
    LlmClient,
    _usage_as_dict,
    extract_llm_usage,
)
from csagent.memory.budget import BudgetAllocation
from csagent.memory.context_pipeline import ContextFrame


def _settings() -> MagicMock:
    s = MagicMock()
    s.llm_api_key = "test-key"
    s.llm_base_url = "https://example.com/v1"
    s.llm_model = "test-model"
    s.llm_timeout_seconds = 30
    s.llm_max_retries = 1
    s.llm_temperature = 0.2
    s.llm_json_mode = False
    s.llm_log_bodies = False
    s.llm_log_bodies_dir = "data/llm_bodies"
    s.llm_max_tokens = 131072
    return s


# ---------- 解析 ----------


def test_extract_llm_usage_reads_openai_fields():
    prompt, completion = extract_llm_usage(
        {"usage": {"prompt_tokens": 12, "completion_tokens": 4}}
    )
    assert (prompt, completion) == (12, 4)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"usage": None},
        {"usage": "nope"},
        {"usage": {"prompt_tokens": "x", "completion_tokens": -3}},
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    ],
)
def test_extract_llm_usage_missing_or_invalid_is_zero(payload):
    assert extract_llm_usage(payload) == (0, 0)


def test_usage_as_dict_omits_all_zero():
    assert _usage_as_dict((0, 0)) is None
    assert _usage_as_dict((9, 0)) == {"prompt_tokens": 9, "completion_tokens": 0}


# ---------- 缓冲合计 + 审计抄写 ----------


def test_note_llm_call_accumulates_across_rounds():
    buf = AuditTraceBuffer()
    buf.note_llm_call(prompt_tokens=10, completion_tokens=2)
    buf.note_llm_call(prompt_tokens=7, completion_tokens=3)
    assert buf.llm_turn_count == 2
    assert buf.prompt_tokens == 17
    assert buf.completion_tokens == 5


def test_fill_from_buffer_copies_tokens_and_layers(tmp_path):
    buf = AuditTraceBuffer()
    buf.note_llm_call(prompt_tokens=20, completion_tokens=6)
    buf.set_context_layers({"system": 40, "checkpoint": 12})

    logger = SessionAuditLogger(audit_dir=tmp_path, enabled=True)
    record = SessionAuditRecord(
        trace_id="t1",
        session_id="s",
        channel="http_api",
        user_message="hi",
        final_reply="ok",
        status="success",
    )
    logger.fill_from_buffer(record, buf)
    assert record.prompt_tokens == 20
    assert record.completion_tokens == 6
    assert record.context_layers == {"system": 40, "checkpoint": 12}


# ---------- 非流式 / 流式接线 ----------


class _StreamResponse:
    """最小可用的 httpx 流式响应替身。"""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCM:
    def __init__(self, response: _StreamResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class _FakeHttp:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.last_json: dict | None = None

    def stream(self, method: str, path: str, json=None):
        self.last_json = json
        return _StreamCM(_StreamResponse(self.lines))


def _sse(*objs: dict) -> list[str]:
    import json as _json

    lines = [f"data: {_json.dumps(obj, ensure_ascii=False)}" for obj in objs]
    lines.append("data: [DONE]")
    return lines


@pytest.mark.asyncio
async def test_non_stream_chat_records_usage(monkeypatch: pytest.MonkeyPatch):
    client = LlmClient(_settings())
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")

    async def _ok_post(*args, **kwargs):
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"role": "assistant", "content": "好的"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )

    fake = MagicMock()
    fake.post = _ok_post
    monkeypatch.setattr(client, "_ensure_client", lambda: fake)

    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    try:
        text = await client.chat([{"role": "user", "content": "hi"}])
    finally:
        audit_trace_var.reset(token)

    assert text == "好的"
    assert buf.llm_turn_count == 1
    assert buf.prompt_tokens == 11
    assert buf.completion_tokens == 5


@pytest.mark.asyncio
async def test_stream_last_nonzero_usage_overwrites(monkeypatch: pytest.MonkeyPatch):
    """中间片没有 usage；末片给累计值。客户端覆盖，不加增量。"""
    client = LlmClient(_settings())
    fake = _FakeHttp(
        _sse(
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
            {
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            },
        )
    )
    monkeypatch.setattr(client, "_ensure_client", lambda: fake)

    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    try:
        text = await client.chat_stream([{"role": "user", "content": "hi"}])
    finally:
        audit_trace_var.reset(token)

    assert text == "你好"
    assert fake.last_json is not None
    assert fake.last_json.get("stream_options") == {"include_usage": True}
    assert buf.prompt_tokens == 40
    assert buf.completion_tokens == 8
    assert buf.llm_turn_count == 1


@pytest.mark.asyncio
async def test_maybe_audit_records_even_when_usage_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """对端没给 usage 也要记一轮调用，用量保持 0——不能假装这次没打过模型。"""
    client = LlmClient(_settings())
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")

    async def _ok_post(*args, **kwargs):
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    fake = MagicMock()
    fake.post = _ok_post
    monkeypatch.setattr(client, "_ensure_client", lambda: fake)

    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    try:
        await client.chat([{"role": "user", "content": "hi"}])
    finally:
        audit_trace_var.reset(token)

    assert buf.llm_turn_count == 1
    assert buf.prompt_tokens == 0
    assert buf.completion_tokens == 0


# ---------- 各层占用 ----------


def test_record_context_layers_writes_budget_and_measured_sizes():
    from csagent.agent import Agent

    agent = Agent(
        react_loop=MagicMock(),
        tool_registry=MagicMock(),
        short_term_memory=MagicMock(),
    )
    frame = ContextFrame(
        context_summary="最近两轮对话原文",
        checkpoint_summary="更早的检查点摘要",
        persistent_instructions="持久规则",
        allocation=BudgetAllocation(
            system_prompt_tokens=10,
            tools_tokens=5,
            recent_history_tokens=20,
            tool_history_tokens=8,
            summary_tokens=40,
            response_reserve=100,
            total_budget=1000,
            needs_compact=False,
        ),
    )
    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    try:
        agent._record_context_layers(frame)
    finally:
        audit_trace_var.reset(token)

    assert buf.context_layers["system"] == 10
    assert buf.context_layers["summary_budget"] == 40
    assert buf.context_layers["checkpoint"] > 0
    assert buf.context_layers["context_summary"] > 0
    assert buf.context_layers["persistent"] > 0


def test_record_context_layers_is_noop_without_buffer():
    from csagent.agent import Agent

    agent = Agent(
        react_loop=MagicMock(),
        tool_registry=MagicMock(),
        short_term_memory=MagicMock(),
    )
    # 没有 contextvar 缓冲时不能抛——护栏拦截、装配测试都走这条路。
    agent._record_context_layers(ContextFrame())
