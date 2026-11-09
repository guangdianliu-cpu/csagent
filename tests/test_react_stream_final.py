# -*- coding: utf-8 -*-
"""ReAct 纯文本终答在有 on_delta 时应直接输出（不额外调 LLM 重写）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from csagent.react.loop import ReactLoop
from csagent.react.models import CustomerScope, DomainTaskRequest
from csagent.tools.registry import ToolOutcome


def _tool_call_msg(tool_name: str, arguments: dict, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _plain_text_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text, "tool_calls": None}


class _StreamLlm:
    def __init__(self) -> None:
        self.chat_calls = 0
        self.stream_calls = 0
        self._tool_replies: list[dict[str, Any]] = [
            _tool_call_msg("trade_query", {"entity": "available_shares"}),
            _plain_text_msg("草稿持仓说明"),
        ]

    async def chat(self, messages, **kwargs):
        self.chat_calls += 1
        return "fallback"

    async def chat_stream(self, messages, *, on_delta=None, **kwargs):
        self.stream_calls += 1
        text = "流式最终回复：您持有测试基金。"
        for i in range(0, len(text), 4):
            piece = text[i : i + 4]
            if on_delta is not None:
                await on_delta(piece)
        return text

    async def chat_with_tools(self, messages, tools, **kwargs) -> dict[str, Any] | None:
        self.chat_calls += 1
        if not self._tool_replies:
            return None
        return self._tool_replies.pop(0)


class _Tools:
    async def execute_detailed(self, tool_name, arguments, scope, agent_name):
        return ToolOutcome("mock rows")

    def list_tool_schemas_for_agent(self, agent_name: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "trade_query",
                    "description": "查询交易数据",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
        ]


@pytest.mark.asyncio
async def test_plain_text_uses_direct_output_when_on_delta():
    """纯文本终答直接推送，不再额外调 chat_stream 重写。"""
    llm = _StreamLlm()
    loop = ReactLoop(llm_client=llm, tool_registry=_Tools())
    deltas: list[str] = []

    async def on_delta(t: str) -> None:
        deltas.append(t)

    result = await loop.run(
        DomainTaskRequest(
            task_id="t1",
            domain="trade",
            instruction="查持仓",
            customer_scope=CustomerScope(session_id="s1"),
            max_rounds=8,
            trace_id="tr",
        ),
        system_prompt="sys",
        agent_name="trade_agent",
        on_delta=on_delta,
    )

    # chat_with_tools 被调用 2 次（tool + 纯文本终答），chat_stream 不应被调用
    assert llm.stream_calls == 0
    assert llm.chat_calls == 2
    assert "".join(deltas) == "草稿持仓说明"
    assert result.answer_draft == "草稿持仓说明"
    assert len(deltas) == 1  # 整段一次输出
