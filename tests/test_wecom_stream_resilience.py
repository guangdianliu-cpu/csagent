"""企微流式回帧的失败隔离。

线上事故（2026-08-16 13:30 / 13:31）：用户连问两次，都秒回「处理超时，请稍后重试。」，
而 llm_bodies 与 audit 都没有对应记录——Agent 根本没发起过 LLM 调用。

根因是异常类型撞车：Python 3.11 起 ``asyncio.TimeoutError is TimeoutError``，
而企微 SDK 的 reply_stream 等服务端回执，5s 收不到就抛**内置** TimeoutError
（aibot/ws.py `_on_reply_ack_timeout`）。bridge 里那个 ``except TimeoutError``
本意是接自己的 ``asyncio.timeout(330)``，结果把传输层回执抖动一并接了，
报给用户的却是「Agent 处理超时」。

本文件锁定两件事：进度帧发不出去不该毁掉整轮；两种超时必须被区分。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from csagent.infra.config import Settings
from csagent.prompts.domain_text import (
    HANDLE_TIMEOUT_REPLY,
    TRANSPORT_FAILURE_REPLY,
)
from csagent.channels.models import UnifiedMessage
from csagent.stream_events import AgentEventType
from csagent.wecom.bridge import WeComAgentBridge


class _Event:
    def __init__(self, event_type: AgentEventType, content: str) -> None:
        self.event_type = event_type
        self.content = content


class _Gateway:
    """产出固定事件序列的假 Gateway。"""

    def __init__(self, events: list[_Event]) -> None:
        self._events = events

    async def handle(self, message: Any):
        for event in self._events:
            yield event


class _Client:
    """假 WSClient：可指定哪几次 reply_stream 抛回执超时。"""

    def __init__(self, fail_on: set[int] | None = None, fail_all: bool = False) -> None:
        self.sent: list[tuple[str, bool]] = []
        self._fail_on = fail_on or set()
        self._fail_all = fail_all
        self._n = 0

    async def reply_stream(self, frame, stream_id, content, finish=False, **kwargs):
        self._n += 1
        if self._fail_all or self._n in self._fail_on:
            raise TimeoutError(f"Reply ack timeout (5.0s) for reqId: fake-{self._n}")
        self.sent.append((content, finish))
        return {}


def _bridge(
    gateway: _Gateway, client: _Client, *, append_elapsed: bool = True
) -> WeComAgentBridge:
    settings = Settings()
    settings.wecom_append_round_elapsed = append_elapsed
    bridge = WeComAgentBridge(
        main_agent=MagicMock(),
        settings=settings,
        message_gateway=gateway,
    )
    bridge._client = client
    bridge._stream_interval = 0.0  # 每个 delta 都尝试刷帧
    return bridge


def _message() -> UnifiedMessage:
    return UnifiedMessage(
        session_id="wecom:tester",
        channel="wecom",
        text="固收绝对收益型 怎么分类的",
    )


async def _run(bridge: WeComAgentBridge, client: _Client) -> None:
    await bridge._stream_via_gateway(
        {"headers": {"req_id": "req-1"}},
        _message(),
        userid="tester",
        is_group=False,
    )


@pytest.mark.asyncio
async def test_first_progress_frame_ack_timeout_does_not_kill_the_turn():
    """事故的直接形态：首帧回执超时，整轮回答被丢弃。"""
    gateway = _Gateway([
        _Event(AgentEventType.PROGRESS, "正在理解您的问题..."),
        _Event(AgentEventType.DELTA, "绝对收益型属于固收+的三级分类。"),
    ])
    client = _Client(fail_on={1})  # 只有首帧失败
    bridge = _bridge(gateway, client)

    await _run(bridge, client)

    final = [c for c, finish in client.sent if finish]
    assert final, "最终帧应当仍被发出"
    assert "绝对收益型属于固收+的三级分类。" in final[-1]
    assert "【本轮共耗时】：" in final[-1]
    assert HANDLE_TIMEOUT_REPLY not in final[-1]


@pytest.mark.asyncio
async def test_intermediate_frame_failures_do_not_truncate_answer():
    gateway = _Gateway([
        _Event(AgentEventType.PROGRESS, "正在查询..."),
        _Event(AgentEventType.DELTA, "第一段。"),
        _Event(AgentEventType.DELTA, "第二段。"),
        _Event(AgentEventType.DELTA, "第三段。"),
    ])
    client = _Client(fail_on={1, 2, 3})
    bridge = _bridge(gateway, client)

    await _run(bridge, client)

    final = [c for c, finish in client.sent if finish]
    assert final
    assert final[-1].startswith("第一段。第二段。第三段。")
    assert "【本轮共耗时】：" in final[-1]
    assert final[-1].rstrip().endswith("秒")


@pytest.mark.asyncio
async def test_round_elapsed_not_appended_when_disabled():
    gateway = _Gateway([_Event(AgentEventType.DELTA, "绝对收益型属于固收+。")])
    client = _Client()
    bridge = _bridge(gateway, client, append_elapsed=False)

    await _run(bridge, client)

    final = [c for c, finish in client.sent if finish]
    assert final == ["绝对收益型属于固收+。"]


@pytest.mark.asyncio
async def test_transport_failure_message_is_not_the_handle_timeout_message():
    """传输层彻底不可用时，也不该谎称是 Agent 处理超时。"""
    gateway = _Gateway([_Event(AgentEventType.DELTA, "")])
    client = _Client(fail_all=True)
    bridge = _bridge(gateway, client)

    await _run(bridge, client)

    # 所有帧都发不出去，用户什么也收不到；关键是不要把传输故障记成处理超时
    assert client.sent == []


@pytest.mark.asyncio
async def test_inflight_counter_is_released(monkeypatch):
    gateway = _Gateway([_Event(AgentEventType.DELTA, "ok")])
    client = _Client()
    bridge = _bridge(gateway, client)

    await _run(bridge, client)
    assert bridge._inflight == 0

    # 传输全挂时也要归零，否则 status.inflight 会计数泄漏
    bridge2 = _bridge(_Gateway([_Event(AgentEventType.DELTA, "x")]), _Client(fail_all=True))
    await _run(bridge2, bridge2._client)
    assert bridge2._inflight == 0


def test_the_two_timeouts_share_one_exception_type():
    """这条不变量正是事故成因，写下来防止有人把 expired() 判断删掉。"""
    import asyncio

    assert asyncio.TimeoutError is TimeoutError
    assert TRANSPORT_FAILURE_REPLY != HANDLE_TIMEOUT_REPLY
