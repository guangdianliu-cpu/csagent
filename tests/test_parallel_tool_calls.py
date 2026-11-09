"""react.loop.ReactLoop 并行工具调用（M1.2）测试。

验证同一轮内多个独立业务工具调用会被并发执行（而非串行等待），
以及并发批大小上限 (`max_parallel_tool_calls`) 生效。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from csagent.react.loop import ReactLoop
from csagent.react.models import CustomerScope, DomainTaskRequest
from csagent.tools.registry import ToolOutcome

from tests.test_react_loop import (
    ScriptedLlmClient,
    _final_answer_msg,
)


def _multi_tool_call_msg(calls: list[tuple[str, dict]]) -> dict[str, Any]:
    """构造一条包含多个 tool_calls 的 assistant message。"""
    import json

    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


class SlowStubToolRegistry:
    """每次调用挂起固定时长，用于观测是否并行执行。"""

    def __init__(self, delay: float = 0.1) -> None:
        self._delay = delay
        self.calls: list[tuple[str, dict]] = []
        self.concurrent_peak = 0
        self._active = 0

    async def execute_detailed(self, tool_name, arguments, scope, agent_name):
        self.calls.append((tool_name, arguments))
        self._active += 1
        self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            await asyncio.sleep(self._delay)
        finally:
            self._active -= 1
        return ToolOutcome(f"工具{tool_name}返回: ok")

    def list_tool_schemas_for_agent(self, agent_name: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "测试工具",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
            for i in range(5)
        ]


def _make_request(max_rounds: int = 8) -> DomainTaskRequest:
    return DomainTaskRequest(
        task_id="t1",
        domain="trade",
        instruction="并行查询多个数据源",
        customer_scope=CustomerScope(session_id="s1"),
        max_rounds=max_rounds,
        trace_id="trace1",
    )


@pytest.mark.asyncio
async def test_independent_tool_calls_run_concurrently():
    """同一轮内 3 个独立工具调用应并发执行，总耗时接近单次耗时而非 3 倍。"""
    llm = ScriptedLlmClient([
        _multi_tool_call_msg(
            [("tool_0", {}), ("tool_1", {}), ("tool_2", {})]
        ),
        _final_answer_msg("已完成并行查询"),
    ])
    tools = SlowStubToolRegistry(delay=0.15)
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_parallel_tool_calls=5)

    start = time.monotonic()
    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")
    elapsed = time.monotonic() - start

    assert result.status == "success"
    assert len(tools.calls) == 3
    # 并行执行：峰值并发应 > 1（串行版本恒为 1）
    assert tools.concurrent_peak > 1
    # 总耗时应明显小于串行 3 次耗时（0.45s），留足余量避免抖动误判
    assert elapsed < 0.4


@pytest.mark.asyncio
async def test_parallel_batch_respects_max_parallel_limit():
    """当 max_parallel_tool_calls=1 时应退化为原有的顺序执行行为。"""
    llm = ScriptedLlmClient([
        _multi_tool_call_msg(
            [("tool_0", {}), ("tool_1", {}), ("tool_2", {})]
        ),
        _final_answer_msg("已完成"),
    ])
    tools = SlowStubToolRegistry(delay=0.05)
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_parallel_tool_calls=1)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert len(tools.calls) == 3
    assert tools.concurrent_peak == 1


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_tool_error_and_continues():
    """未知工具名（含已下线的 __final_answer__）不截断批次，下一轮纯文本收口。"""
    import json

    mixed_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "tool_0", "arguments": json.dumps({})},
            },
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "tool_1", "arguments": json.dumps({})},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "__final_answer__", "arguments": json.dumps({})},
            },
        ],
    }
    llm = ScriptedLlmClient([mixed_msg, _final_answer_msg("合并结果已给出")])
    tools = SlowStubToolRegistry(delay=0.02)
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_parallel_tool_calls=5)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert result.answer_draft == "合并结果已给出"
    assert len(tools.calls) == 3
