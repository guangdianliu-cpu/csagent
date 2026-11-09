"""工具调用上下文记忆测试。

覆盖两层：
1. `SessionSnapshotStore.append_tool_calls` / `load_tool_calls` 的存取与淘汰语义。
2. `Agent` 端到端：真实 ReactLoop 执行一次工具调用后，
   `Agent._append_tool_call_snapshots` 应把该调用写入会话存储，
   供下一轮对话通过 ContextPipeline 回溯。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.agent import Agent
from csagent.guardrail.input_guard import GuardrailVerdict
from csagent.memory.session_snapshot import SessionSnapshotStore, ToolCallSnapshot
from csagent.memory.short_term import ShortTermMemory
from csagent.react.loop import ReactLoop

from tests.test_react_loop import ScriptedLlmClient, StubToolRegistry, _final_answer_msg, _tool_call_msg


class _FakeInputGuard:
    async def review(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(passed=True, violations=[])


class _FakeOutputGuard:
    def review(self, text: str):
        from csagent.guardrail.output_guard import OutputGuardVerdict

        return OutputGuardVerdict(passed=True, sanitized_text=text)


# ---------------------------------------------------------------------------
# SessionSnapshotStore 单元测试
# ---------------------------------------------------------------------------

def test_append_and_load_tool_calls_round_trip():
    store = SessionSnapshotStore()
    store.append_tool_calls(
        "s1",
        [ToolCallSnapshot(tool_name="grep", arguments_summary="foo", result_summary="找到 3 处")],
    )
    history = store.load_tool_calls("s1", last_n=10)
    assert len(history) == 1
    assert history[0].tool_name == "grep"
    assert history[0].result_summary == "找到 3 处"


def test_load_tool_calls_returns_most_recent_n_in_order():
    store = SessionSnapshotStore()
    for i in range(5):
        store.append_tool_calls(
            "s1", [ToolCallSnapshot(tool_name=f"tool_{i}", arguments_summary="", result_summary="")]
        )
    recent = store.load_tool_calls("s1", last_n=2)
    assert [tc.tool_name for tc in recent] == ["tool_3", "tool_4"]


def test_append_tool_calls_evicts_oldest_beyond_max_entries():
    store = SessionSnapshotStore(max_tool_call_entries=3)
    for i in range(5):
        store.append_tool_calls(
            "s1", [ToolCallSnapshot(tool_name=f"tool_{i}", arguments_summary="", result_summary="")]
        )
    all_history = store.load_tool_calls("s1", last_n=100)
    assert len(all_history) == 3
    assert all_history[0].tool_name == "tool_2"


def test_clear_removes_tool_call_history_too():
    store = SessionSnapshotStore()
    store.append_tool_calls(
        "s1", [ToolCallSnapshot(tool_name="grep", arguments_summary="", result_summary="")]
    )
    store.clear("s1")
    assert store.load_tool_calls("s1") == []


def test_load_tool_calls_unknown_session_returns_empty():
    store = SessionSnapshotStore()
    assert store.load_tool_calls("does-not-exist") == []


def test_session_count_capped_with_lru_eviction():
    """会话数超过上限时淘汰最久未写入的会话（进程内后端没有 TTL，只有计数 LRU）。"""
    store = SessionSnapshotStore(max_sessions=2)
    for sid in ("s1", "s2", "s3"):
        store.append_tool_calls(
            sid, [ToolCallSnapshot(tool_name="grep", arguments_summary="", result_summary="hit")]
        )
    assert store.load_tool_calls("s1") == []
    assert store.load_tool_calls("s2") != []
    assert store.load_tool_calls("s3") != []


# ---------------------------------------------------------------------------
# Agent 端到端：真实工具调用后应写入快照，供下一轮回溯
# ---------------------------------------------------------------------------

def _build_agent(react_loop: ReactLoop, snapshot_store: SessionSnapshotStore) -> Agent:
    return Agent(
        react_loop=react_loop,
        tool_registry=MagicMock(),
        short_term_memory=ShortTermMemory(),
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        session_snapshot_store=snapshot_store,
        show_thinking_process=False,
    )


@pytest.mark.asyncio
async def test_successful_tool_call_is_persisted_to_snapshot_store():
    llm = ScriptedLlmClient([
        _tool_call_msg("trade_query", {"entity": "available_shares"}, content="查一下"),
        _final_answer_msg("您持有XX基金"),
    ])
    tools = StubToolRegistry()
    react_loop = ReactLoop(llm_client=llm, tool_registry=tools)
    snapshot_store = SessionSnapshotStore()
    agent = _build_agent(react_loop, snapshot_store)
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]

    async for _ in agent.handle_message_stream(
        session_id="s-mem", text="帮我查一下持仓"
    ):
        pass

    history = snapshot_store.load_tool_calls("s-mem", last_n=10)
    assert len(history) == 1
    assert history[0].tool_name == "trade_query"
    assert "mock-data" in history[0].result_summary


@pytest.mark.asyncio
async def test_tool_history_is_injected_into_next_turn_context():
    """第二轮请求时，Agent 应把上一轮的工具调用摘要注入本轮 LLM 消息（context_summary）。"""
    llm = ScriptedLlmClient([
        _tool_call_msg("trade_query", {"entity": "available_shares"}, content="查一下"),
        _final_answer_msg("您持有XX基金"),
        # 第二轮：直接收口，不再调用业务工具
        _final_answer_msg("上次已经查过持仓了"),
    ])
    tools = StubToolRegistry()
    react_loop = ReactLoop(llm_client=llm, tool_registry=tools)
    snapshot_store = SessionSnapshotStore()
    agent = _build_agent(react_loop, snapshot_store)
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]

    async for _ in agent.handle_message_stream(
        session_id="s-mem2", text="帮我查一下持仓"
    ):
        pass

    # 第二轮：捕获实际发给 LLM 的首条 user 消息，验证工具历史已注入
    captured_messages: list[list[dict]] = []
    original_chat_with_tools = llm.chat_with_tools

    async def capturing_chat_with_tools(messages, tools_schema, **kwargs):
        captured_messages.append(list(messages))
        return await original_chat_with_tools(messages, tools_schema, **kwargs)

    llm.chat_with_tools = capturing_chat_with_tools  # type: ignore[method-assign]

    async for _ in agent.handle_message_stream(
        session_id="s-mem2", text="上次查询结果是什么"
    ):
        pass

    assert captured_messages, "第二轮应发起过至少一次带工具的 LLM 调用"
    first_round_messages = captured_messages[0]
    user_contents = [
        m.get("content") or ""
        for m in first_round_messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    combined = "\n".join(user_contents)
    assert "trade_query" in combined
    assert "mock-data" in combined


@pytest.mark.asyncio
async def test_snapshot_store_without_append_tool_calls_is_skipped_gracefully():
    """会话存储未实现 append_tool_calls（极简替身）时不应报错。"""

    class _MinimalStore:
        def clear(self, session_id: str) -> None:
            return None

    llm = ScriptedLlmClient([
        _tool_call_msg("trade_query", {"entity": "available_shares"}, content="查一下"),
        _final_answer_msg("您持有XX基金"),
    ])
    tools = StubToolRegistry()
    react_loop = ReactLoop(llm_client=llm, tool_registry=tools)
    agent = _build_agent(react_loop, _MinimalStore())
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]

    events = []
    async for event in agent.handle_message_stream(
        session_id="s-mem3", text="帮我查一下持仓"
    ):
        events.append(event)

    assert events  # 正常产出事件，未抛异常
