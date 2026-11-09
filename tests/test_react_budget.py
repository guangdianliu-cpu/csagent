"""本轮预算感知：开局写进系统提示词，接近上限时在工具结果后提醒模型（react/budget.py）。"""

from __future__ import annotations

import pytest

from csagent.react.budget import BudgetLevel, TurnBudget
from csagent.react.loop import ReactLoop
from tests.test_react_loop import (
    ScriptedLlmClient,
    StubToolRegistry,
    _make_request,
    _plain_text_msg,
    _tool_call_msg,
)


def test_system_prompt_section_lists_all_limits():
    budget = TurnBudget(max_rounds=48, max_tool_calls=80, time_limit_seconds=1500)
    text = budget.system_prompt_section()
    assert "48 轮" in text and "80 次工具调用" in text and "25 分钟" in text


def test_unlimited_items_are_left_out():
    text = TurnBudget(max_rounds=10).system_prompt_section()
    assert "10 轮" in text and "次工具调用" not in text and "分钟" not in text


def test_each_level_is_sent_once_and_in_order():
    budget = TurnBudget(max_rounds=20, max_tool_calls=100, time_limit_seconds=1000)
    assert budget.notice(1, 1, now=10) is None
    warn = budget.notice(14, 14, now=100)  # 轮数 70%
    assert warn is not None and "收敛" in warn and "14/20 轮" in warn
    assert budget.notice(15, 15, now=110) is None  # 同级不重复
    final = budget.notice(16, 16, now=920)  # 时间 92%
    assert final is not None and "不要再调用工具" in final
    assert budget.sent_level == BudgetLevel.FINAL
    assert budget.notice(17, 17, now=990) is None


def test_time_or_tool_calls_alone_can_trigger():
    by_time = TurnBudget(max_rounds=48, time_limit_seconds=1500)
    assert by_time.level(2, 0, now=1100) == BudgetLevel.WARN
    by_calls = TurnBudget(max_rounds=48, max_tool_calls=80)
    assert by_calls.level(2, 73, now=0) == BudgetLevel.FINAL


def test_last_rounds_trigger_final_even_with_small_limit():
    budget = TurnBudget(max_rounds=4)
    assert budget.level(1, 1, now=0) == BudgetLevel.OK
    assert budget.level(2, 2, now=0) == BudgetLevel.FINAL  # 只剩 2 轮


@pytest.mark.asyncio
async def test_loop_puts_budget_in_system_prompt_and_warns_on_tool_result():
    seen: list[list[dict]] = []

    class _Recording(ScriptedLlmClient):
        async def chat_with_tools(self, messages, tools, **kwargs):
            seen.append([dict(m) for m in messages])
            return await super().chat_with_tools(messages, tools, **kwargs)

    llm = _Recording([
        _tool_call_msg("query_trade", {"n": 1}, call_id="c1"),
        _tool_call_msg("query_trade", {"n": 2}, call_id="c2"),
        _plain_text_msg("完成"),
    ])
    loop = ReactLoop(llm_client=llm, tool_registry=StubToolRegistry(), max_tool_calls=50,
                     overall_timeout_seconds=None)
    result = await loop.run(_make_request(max_rounds=6), system_prompt="sys", agent_name="a")

    assert result.answer_draft == "完成"
    assert "本轮预算" in seen[0][0]["content"] and "6 轮" in seen[0][0]["content"]
    tool_msgs = [m for m in seen[-1] if m.get("role") == "tool"]
    # 第 1 轮后 1/6 未到阈值；第 2 轮后 2/6 且只剩 4 轮也未到；不提醒
    assert all("本轮预算]" not in m["content"] for m in tool_msgs)


@pytest.mark.asyncio
async def test_loop_sends_final_notice_before_last_rounds():
    seen: list[list[dict]] = []

    class _Recording(ScriptedLlmClient):
        async def chat_with_tools(self, messages, tools, **kwargs):
            seen.append([dict(m) for m in messages])
            return await super().chat_with_tools(messages, tools, **kwargs)

    llm = _Recording([
        _tool_call_msg("query_trade", {"n": 1}, call_id="c1"),
        _tool_call_msg("query_trade", {"n": 2}, call_id="c2"),
        _plain_text_msg("完成"),
    ])
    loop = ReactLoop(llm_client=llm, tool_registry=StubToolRegistry(), max_tool_calls=50,
                     overall_timeout_seconds=None)
    await loop.run(_make_request(max_rounds=4), system_prompt="sys", agent_name="a")

    tool_msgs = [m for m in seen[-1] if m.get("role") == "tool"]
    assert "本轮预算]" not in tool_msgs[0]["content"]  # 1/4，剩 3 轮
    assert "不要再调用工具" in tool_msgs[1]["content"]  # 剩 2 轮
    assert tool_msgs[1]["content"].startswith("工具query_trade返回")


@pytest.mark.parametrize("overall, expected", [(1500.0, 1500.0 + 240.0 + 60.0), (0, None)])
def test_wecom_timeout_follows_react_deadline(overall, expected):
    """企微一轮的兜底超时排在 ReAct 硬截止之后，不再先于 ReAct 掐断；不限时则不设。"""
    from unittest.mock import MagicMock

    from csagent.react.loop import HARD_TIMEOUT_GRACE_SECONDS
    from csagent.wecom.bridge import WeComAgentBridge

    settings = MagicMock()
    settings.wecom_stream_interval_ms = 10
    settings.react_overall_timeout_seconds = overall
    bridge = WeComAgentBridge(main_agent=MagicMock(), settings=settings,
                              message_gateway=MagicMock())
    assert bridge._handle_timeout == expected
    if expected is not None:
        assert bridge._handle_timeout > overall + HARD_TIMEOUT_GRACE_SECONDS
