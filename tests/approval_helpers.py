"""确认卡片相关测试共用的工具（本文件不是测试，pytest 不收集）。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from csagent.agent import Agent, ApprovedAction
from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.guardrail.output_guard import OutputGuard
from csagent.memory.short_term import ShortTermMemory
from csagent.react.models import CustomerScope
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.tools.approvals import ApprovalStore, PendingApproval
from csagent.tools.base import BaseTool
from csagent.tools.policy import ToolPolicy
from csagent.tools.registry import ToolRegistry
from csagent.wecom.bridge import WeComAgentBridge
from csagent.wecom.cards import CANCEL_ACTION, CONFIRM_ACTION

# 群聊里发文件需要确认
CHALLENGE_POLICY = """
policy:
  version: "1"
  rules:
    - id: group-send
      effect: challenge
      reason: 群里可见
      when: {tool: send_file, chat_type: group}
"""
# 卡片发出后策略改成拒绝
DENY_POLICY = """
policy:
  version: "2"
  rules:
    - id: no-send
      effect: deny
      when: {tool: send_file}
"""


class RecordingTool(BaseTool):
    """记下调用次数、返回固定结果（或抛错）的工具。"""

    def __init__(self, name: str = "send_file", result: str = "ok", boom: bool = False) -> None:
        self.name = name
        self.description = "test"
        self.calls = 0
        self._result = result
        self._boom = boom

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        self.calls += 1
        if self._boom:
            raise RuntimeError("tool exploded")
        return self._result


def group_scope(**overrides: Any) -> CustomerScope:
    """企微群聊里 owner 发起的一轮。"""
    base: dict[str, Any] = dict(
        session_id="wecom:group:g1:owner", channel="wecom", user_id="owner",
        chat_type="group", chat_id="g1", auth_method="wecom",
    )
    base.update(overrides)
    return CustomerScope(**base)


def challenge_registry(
    tool: BaseTool | None = None,
    *,
    store: ApprovalStore | None = None,
    policy: str = CHALLENGE_POLICY,
) -> ToolRegistry:
    """挂一个 send_file、按 ``policy`` 判定的注册中心。"""
    return ToolRegistry(
        [tool or RecordingTool()],
        {"agent": ["send_file"]},
        policy=ToolPolicy.from_text(policy),
        approvals=store if store is not None else ApprovalStore(),
    )


async def raise_card(
    registry: ToolRegistry, scope: CustomerScope, path: str = "a.xlsx"
) -> PendingApproval:
    """在群聊里发文件触发一张卡片，返回确认簿里登记的那一条。"""
    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        outcome = await registry.execute_detailed("send_file", {"path": path}, scope, "agent")
    finally:
        audit_trace_var.reset(token)
    assert outcome.status == "challenged"
    assert registry.approvals is not None
    return next(
        item
        for item in registry.approvals.list_pending(scope.session_id)
        if item.arguments == {"path": path}
    )


def make_agent(registry: ToolRegistry, guard: OutputGuard | None = None) -> Agent:
    """只用来执行已批准操作的 Agent：ReAct 循环与审计落盘都打桩。"""
    loop = MagicMock()
    loop._llm_client = None
    agent = Agent(
        react_loop=loop, tool_registry=registry, short_term_memory=ShortTermMemory(),
        output_guard=guard, show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]
    return agent


def card_frame(
    task_id: str,
    *,
    operator: str,
    chatid: str,
    chattype: str = "group",
    approve: bool = True,
) -> dict[str, Any]:
    """企微卡片点击回调帧。"""
    return {"body": {
        "from": {"userid": operator}, "chatid": chatid, "chattype": chattype,
        "event": {"template_card_event": {
            "task_id": task_id,
            "event_key": CONFIRM_ACTION if approve else CANCEL_ACTION,
        }},
    }}


class ResumeGateway:
    """续跑那一轮：吐两段正文和一张（确认簿里没有的）卡片事件。"""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def handle(self, message: Any):
        self.messages.append(message)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="报表已发到群里，")
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="接下来还要外呼。")
        yield AgentStreamEvent(event_type=AgentEventType.CHALLENGE, content=json.dumps(
            {"task_id": "task_next", "tool": "http_call", "summary": "外呼", "user_id": "owner",
             "chat_type": "group"}, ensure_ascii=False))


def wecom_bridge(store: ApprovalStore, gateway: Any = None) -> tuple[WeComAgentBridge, MagicMock]:
    """接一个假企微客户端的桥；Agent 的执行按真实约定记结果、返回 ApprovedAction。"""
    main_agent = MagicMock()
    main_agent.approvals = store

    async def _execute(pending: PendingApproval) -> ApprovedAction:
        note = f"[系统] 已执行 {pending.summary}"
        store.complete(pending.task_id, result_text="ok", result_status="success",
                       continuation=note)
        return ApprovedAction(task_id=pending.task_id, status="success", text="ok",
                              continuation=note)

    main_agent.execute_approved_action = AsyncMock(side_effect=_execute)
    settings = MagicMock()
    settings.wecom_stream_interval_ms = 10
    settings.wecom_welcome = ""
    bridge = WeComAgentBridge(
        main_agent=main_agent, settings=settings, message_gateway=gateway or ResumeGateway()
    )
    bridge._client = MagicMock()
    bridge._client.update_template_card = AsyncMock()
    bridge._client.reply_template_card = AsyncMock()
    bridge._client.send_message = AsyncMock()
    main_agent.client = bridge._client
    return bridge, main_agent


async def click_card(registry: ToolRegistry, frame: dict[str, Any], gateway: Any = None) -> MagicMock:
    """在企微里点一次卡片，返回假 Agent（``.client`` 是假企微客户端）。"""
    assert registry.approvals is not None
    bridge, main_agent = wecom_bridge(registry.approvals, gateway)
    await bridge._handle_template_card_event(frame)
    return main_agent


def sent_bodies(client: MagicMock) -> list[dict[str, Any]]:
    """假企微客户端主动发出的消息体（按发送先后）。"""
    return [
        call.args[1] if len(call.args) > 1 else call.kwargs["body"]
        for call in client.send_message.await_args_list
    ]
