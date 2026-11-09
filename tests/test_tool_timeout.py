"""test_tool_timeout.py — ToolRegistry 单工具超时（内核审视报告问题 F）。

改造前 ToolRegistry.execute 是裸 await：一个工具卡住 170 秒，整体 300s 预算只剩
30 秒给后续所有操作。现在每个工具有独立超时，超时只让这一个调用失败，循环继续。
"""

from __future__ import annotations

import asyncio

import pytest

from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool
from csagent.tools.registry import ToolRegistry

AGENT = "agent"


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


class _SlowTool(BaseTool):
    name = "slow_tool"
    description = "睡很久的工具"

    def __init__(self, delay: float = 5.0) -> None:
        self._delay = delay
        self.cancelled = False

    async def execute(self, arguments: dict, scope: CustomerScope) -> str:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "不该跑到这里"


class _FastTool(BaseTool):
    name = "fast_tool"
    description = "立刻返回的工具"

    async def execute(self, arguments: dict, scope: CustomerScope) -> str:
        return "ok"


class _SelfDeclaredTool(BaseTool):
    """自己声明超时的工具：注册中心默认值不该盖掉它。"""

    name = "self_declared"
    description = "自带超时声明"
    timeout_seconds = 0.05

    async def execute(self, arguments: dict, scope: CustomerScope) -> str:
        await asyncio.sleep(5.0)
        return "不该跑到这里"


@pytest.mark.asyncio
async def test_slow_tool_times_out_and_returns_readable_error():
    slow = _SlowTool()
    registry = ToolRegistry.from_tools_and_agent([slow], AGENT, default_timeout_seconds=0.05)

    result = await registry.execute("slow_tool", {}, _scope(), agent_name=AGENT)

    assert "超时" in result
    assert "slow_tool" in result
    # 超时必须真的取消协程，不能放任它在后台继续跑
    assert slow.cancelled is True


@pytest.mark.asyncio
async def test_timeout_does_not_affect_other_tools():
    """一个工具超时不影响同批其他工具——这正是问题 F 要解决的。"""
    registry = ToolRegistry.from_tools_and_agent(
        [_SlowTool(), _FastTool()], AGENT, default_timeout_seconds=0.05
    )

    slow_result, fast_result = await asyncio.gather(
        registry.execute("slow_tool", {}, _scope(), agent_name=AGENT),
        registry.execute("fast_tool", {}, _scope(), agent_name=AGENT),
    )

    assert "超时" in slow_result
    assert fast_result == "ok"


@pytest.mark.asyncio
async def test_tool_declared_timeout_wins_over_registry_default():
    registry = ToolRegistry.from_tools_and_agent(
        [_SelfDeclaredTool()], AGENT, default_timeout_seconds=30.0
    )

    result = await asyncio.wait_for(
        registry.execute("self_declared", {}, _scope(), agent_name=AGENT), timeout=2.0
    )

    assert "超时" in result
    assert "0.05s" in result


@pytest.mark.asyncio
async def test_call_site_timeout_overrides_everything():
    registry = ToolRegistry.from_tools_and_agent(
        [_SlowTool()], AGENT, default_timeout_seconds=30.0
    )

    result = await registry.execute(
        "slow_tool", {}, _scope(), agent_name=AGENT, timeout=0.05
    )

    assert "超时" in result


@pytest.mark.asyncio
async def test_timeout_disabled_when_none():
    """default_timeout_seconds=None 时退回裸 await（保留旧行为的逃生门）。"""
    registry = ToolRegistry.from_tools_and_agent(
        [_SlowTool(delay=0.01)], AGENT, default_timeout_seconds=None
    )

    assert await registry.execute("slow_tool", {}, _scope(), agent_name=AGENT) == "不该跑到这里"


@pytest.mark.asyncio
async def test_normal_tool_unaffected():
    registry = ToolRegistry.from_tools_and_agent([_FastTool()], AGENT)

    assert await registry.execute("fast_tool", {}, _scope(), agent_name=AGENT) == "ok"
