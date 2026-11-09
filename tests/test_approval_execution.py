"""卡片批准后的执行（Agent.execute_approved_action）：审计、脱敏、续跑说明、失败收尾。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.guardrail.output_guard import OutputGuard
from csagent.guardrail.rules import OutputRule
from csagent.tools.approvals import ApprovalStatus, ClaimReason

from tests.approval_helpers import (
    RecordingTool,
    challenge_registry,
    group_scope,
    make_agent,
    raise_card,
)


@pytest.mark.asyncio
async def test_approved_action_is_audited_masked_and_yields_continuation() -> None:
    tool = RecordingTool(result="已发送，联系人 13812345678")
    registry = challenge_registry(tool)
    store = registry.approvals
    pending = await raise_card(registry, group_scope())
    assert store.claim(pending.task_id, "owner").reason == ClaimReason.OK

    guard = OutputGuard([OutputRule(name="phone", action="mask",
                                    pattern=r"(?<!\d)1[3-9]\d{9}(?!\d)", mask_with="1***")])
    agent = make_agent(registry, guard)
    action = await agent.execute_approved_action(pending)

    assert tool.calls == 1
    assert action.status == "success"
    assert "13812345678" not in action.text
    kwargs = agent._write_audit.await_args.kwargs
    assert kwargs["status"] == "approved_action"
    assert kwargs["scope"].user_id == "owner" and kwargs["scope"].chat_id == "g1"
    # 不能再点；续跑说明带着执行结果，只能取一次，且只给本会话
    assert store.claim(pending.task_id, "owner").reason == ClaimReason.DUPLICATE
    assert store.take_continuation("wecom:group:g2:owner", pending.task_id) is None
    taken = store.take_continuation(pending.session_id, pending.task_id)
    assert taken is not None and "已发送" in taken.continuation
    assert "不要再重复调用" in taken.continuation
    assert store.take_continuation(pending.session_id, pending.task_id) is None


@pytest.mark.asyncio
async def test_approved_action_records_tool_call_in_its_own_audit_buffer() -> None:
    registry = challenge_registry()
    pending = await raise_card(registry, group_scope())
    registry.approvals.claim(pending.task_id, "owner")
    agent = make_agent(registry)
    captured: dict = {}

    async def _capture(**kwargs):
        buf = audit_trace_var.get()
        captured["calls"] = list(buf.tool_calls) if buf else []

    agent._write_audit = _capture  # type: ignore[method-assign]
    await agent.execute_approved_action(pending)
    assert captured["calls"] and captured["calls"][0]["decision"] == "approved"
    assert captured["calls"][0]["rule_id"] == "group-send"


@pytest.mark.asyncio
async def test_failed_approved_action_continues_but_is_not_replayed() -> None:
    registry = challenge_registry(RecordingTool(boom=True))
    store = registry.approvals
    pending = await raise_card(registry, group_scope())
    store.claim(pending.task_id, "owner")
    action = await make_agent(registry).execute_approved_action(pending)
    assert action.status != "success"
    assert action.text.startswith("操作未完成")
    # 失败也续跑（让模型解释），但不回放：用户再要求一次时重新出卡片
    taken = store.take_continuation(pending.session_id, pending.task_id)
    assert taken is not None and "没有成功" in taken.continuation
    assert store.executed(pending.session_id, "send_file", {"path": "a.xlsx"}) is None
    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        again = await registry.execute_detailed("send_file", {"path": "a.xlsx"}, group_scope(), "agent")
    finally:
        audit_trace_var.reset(token)
    assert again.status == "challenged"


@pytest.mark.asyncio
async def test_crash_during_approved_action_releases_card() -> None:
    registry = challenge_registry()
    store = registry.approvals
    pending = await raise_card(registry, group_scope())
    store.claim(pending.task_id, "owner")
    agent = make_agent(registry)
    agent._write_audit = AsyncMock(side_effect=RuntimeError("disk full"))  # type: ignore[method-assign]
    # complete 已在审计之前完成：结果保留，续跑照常
    with pytest.raises(RuntimeError):
        await agent.execute_approved_action(pending)
    assert store.lookup(pending.task_id).status == ApprovalStatus.DONE

    registry.execute_approved = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    second = store.request(pending.session_id, "send_file", {"path": "c.xlsx"},
                           summary="x", rule_id="r", user_id="owner")
    store.claim(second.task_id, "owner")
    with pytest.raises(RuntimeError):
        await agent.execute_approved_action(second)
    assert store.lookup(second.task_id) is None


@pytest.mark.asyncio
async def test_unrecorded_result_returns_no_continuation() -> None:
    """操作执行了但卡片已不在确认簿里：不给渠道续跑说明，免得续跑请求落空。"""
    tool = RecordingTool()
    registry = challenge_registry(tool)
    store = registry.approvals
    pending = await raise_card(registry, group_scope())
    store.claim(pending.task_id, "owner")
    store.finish(pending.task_id)  # 模拟结果落不下来（卡片已作废）
    action = await make_agent(registry).execute_approved_action(pending)
    assert tool.calls == 1
    assert action.continuation == ""
    assert action.text == "ok"
