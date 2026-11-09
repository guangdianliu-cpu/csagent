"""流式失败回落：思考不重复推送；429 按 Retry-After 退避后再回落。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from csagent.infra import llm_client as mod
from csagent.infra.llm_client import LlmClient, _retry_after_seconds


def _settings() -> MagicMock:
    s = MagicMock()
    s.llm_api_key = "k"
    s.llm_base_url = "https://example.com/v1"
    s.llm_model = "m"
    s.llm_timeout_seconds = 30
    s.llm_max_retries = 2
    s.llm_temperature = 0.2
    s.llm_json_mode = False
    s.llm_log_bodies = False
    s.llm_log_bodies_dir = "data/llm_bodies"
    s.llm_max_tokens = 1000
    return s


def _client(handler) -> LlmClient:
    c = LlmClient(_settings())
    c._client = httpx.AsyncClient(
        base_url="https://example.com/v1", transport=httpx.MockTransport(handler)
    )
    return c


class _BrokenStream(httpx.AsyncByteStream):
    """先吐一段思考，然后连接断开。"""

    async def __aiter__(self):
        chunk = {"choices": [{"delta": {"reasoning_content": "思考片段"}}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        raise httpx.ReadError("connection reset")


def _final_message():
    return {"choices": [{"message": {
        "role": "assistant", "content": "最终答案", "reasoning_content": "完整思考",
    }}]}


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []

    async def fake_sleep(sec):
        calls.append(sec)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    return calls


async def test_reasoning_not_duplicated_after_midstream_failure(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, stream=_BrokenStream())
        return httpx.Response(200, json=_final_message())

    seen: list[str] = []

    async def on_reasoning(text):
        seen.append(text)

    msg = await _client(handler).chat_with_tools(
        [{"role": "user", "content": "q"}], [], on_reasoning=on_reasoning
    )
    assert msg["content"] == "最终答案"
    # 只推过流式里那一段；回落的非流式不再推「完整思考」
    assert seen == ["思考片段"]


async def test_stream_429_backs_off_with_retry_after(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate"})
        return httpx.Response(200, json=_final_message())

    msg = await _client(handler).chat_with_tools([{"role": "user", "content": "q"}], [])
    assert msg["content"] == "最终答案"
    assert sleeps and 7 <= sleeps[0] < 8


async def test_nonstream_retry_honors_retry_after(sleeps):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(500)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=_final_message())

    msg = await _client(handler).chat_with_tools([{"role": "user", "content": "q"}], [])
    assert msg["content"] == "最终答案"
    assert any(3 <= s < 4 for s in sleeps)


def test_retry_after_parsing():
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "5"})) == 5
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "999"})) == 30
    assert _retry_after_seconds(httpx.Response(429)) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "abc"})) is None
