"""企微流式消息 10 分钟上限：到点先收尾，任务照常跑完，终答用主动消息发出。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.channels.models import UnifiedMessage
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.wecom import bridge as bridge_mod
from csagent.wecom.bridge import WeComAgentBridge


class _Gateway:
    def __init__(self, pause: float) -> None:
        self._pause = pause

    async def handle(self, message):
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="第一部分。")
        await asyncio.sleep(self._pause)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="第二部分。")


def _bridge(gateway) -> tuple[WeComAgentBridge, MagicMock]:
    settings = MagicMock()
    settings.wecom_stream_interval_ms = 10
    settings.wecom_append_round_elapsed = False
    settings.react_overall_timeout_seconds = 1500
    bridge = WeComAgentBridge(main_agent=MagicMock(), settings=settings, message_gateway=gateway)
    client = MagicMock()
    client.reply_stream = AsyncMock()
    client.send_message = AsyncMock()
    bridge._client = client
    return bridge, client


async def _run(bridge, *, is_group: bool):
    await bridge._stream_via_gateway(
        {"headers": {"req_id": "r1"}, "body": {}},
        UnifiedMessage(channel="wecom", session_id="wecom:x", text="长任务"),
        userid="u1",
        is_group=is_group,
        group_chatid="g1" if is_group else "",
    )


@pytest.mark.asyncio
async def test_long_task_closes_stream_then_pushes_full_answer(monkeypatch):
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    monkeypatch.setattr(bridge_mod, "_STREAM_WINDOW_SECONDS", 0.05)
    bridge, client = _bridge(_Gateway(pause=0.2))

    await _run(bridge, is_group=True)

    finishes = [c for c in client.reply_stream.await_args_list if c.kwargs.get("finish")]
    assert len(finishes) == 1  # 只收尾一次，收尾后不再刷流式消息
    closing = finishes[0].args[2]
    assert "第一部分" in closing and "单独发送完整结果" in closing
    assert client.reply_stream.await_args_list[-1] is finishes[0]
    bodies = [c.kwargs.get("body") or c.args[1] for c in client.send_message.await_args_list]
    pushed = "".join(b["markdown"]["content"] for b in bodies if b.get("msgtype") == "markdown")
    assert pushed.startswith("@u1 ") and "<@" not in pushed and "第一部分。第二部分。" in pushed
    chatids = [c.kwargs.get("chatid") or c.args[0] for c in client.send_message.await_args_list]
    assert set(chatids) == {"g1"}


class _ChecklistThenAnswer:
    """先推待办，再推工具进度，最后才出正文。"""

    async def handle(self, message):
        yield AgentStreamEvent(
            event_type=AgentEventType.PROGRESS,
            content="待办\n☐ 查净值\n☐ 写报告",
        )
        yield AgentStreamEvent(
            event_type=AgentEventType.PROGRESS,
            content="正在搜索...",
        )
        yield AgentStreamEvent(
            event_type=AgentEventType.PROGRESS,
            content="待办\n✅ 查净值\n☐ 写报告",
        )
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="净值是 1.23")


class _ChecklistAllDone:
    """清单做完后只剩标题，随后出正文。"""

    async def handle(self, message):
        yield AgentStreamEvent(event_type=AgentEventType.PROGRESS, content="待办\n⏳ 查净值")
        yield AgentStreamEvent(event_type=AgentEventType.PROGRESS, content="正在搜索...")
        yield AgentStreamEvent(event_type=AgentEventType.PROGRESS, content="待办")
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="净值是 1.23")


@pytest.mark.asyncio
async def test_todo_checklist_removed_when_all_done(monkeypatch):
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    bridge, client = _bridge(_ChecklistAllDone())

    await _run(bridge, is_group=False)

    bodies = [c.args[2] for c in client.reply_stream.await_args_list]
    assert "待办\n⏳ 查净值" in bodies
    assert "正在搜索..." not in bodies
    assert bodies[-1] == "净值是 1.23"


@pytest.mark.asyncio
async def test_todo_checklist_ignores_status_line(monkeypatch):
    """有清单之后不再刷「正在…」，气泡高度不跟着进度文案变。终答整段替换。"""
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    bridge, client = _bridge(_ChecklistThenAnswer())

    await _run(bridge, is_group=False)

    bodies = [c.args[2] for c in client.reply_stream.await_args_list]
    assert "待办\n☐ 查净值\n☐ 写报告" in bodies
    assert "待办\n✅ 查净值\n☐ 写报告" in bodies
    assert all("正在搜索" not in text for text in bodies)
    last = client.reply_stream.await_args_list[-1]
    assert last.kwargs.get("finish") is True
    assert last.args[2] == "净值是 1.23"
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_task_finishes_in_stream_without_push(monkeypatch):
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    bridge, client = _bridge(_Gateway(pause=0))

    await _run(bridge, is_group=False)

    last = client.reply_stream.await_args_list[-1]
    assert last.kwargs.get("finish") is True
    assert last.args[2] == "第一部分。第二部分。"
    client.send_message.assert_not_awaited()
