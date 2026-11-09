"""跨层传递的核心协议模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    CALL_TOOL = "call_tool"


class AgentAction(BaseModel):
    """ReAct 单步工具调用动作（仅业务工具，收口走纯文本）。"""

    action: ActionType
    thought: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class CustomerScope(BaseModel):
    """每次工具调用必须携带的身份范围。

    2026-08-21 移除了 customer_id：全链路没有任何地方真的拿它解析过 CUSTNO
    或账户，唯一的消费点是往提示词里写一句「已绑定客户身份」的空头承诺。
    将来若真要做客户维度的能力，重新设计，不要复活这个字段。
    """

    session_id: str
    channel: str = "internal_agent"
    # ---- 请求主体（策略判定与审计用，**不是**客户账户绑定）----
    # 一次工具调用要能回答「谁、经什么渠道、在什么场合、为哪一轮」：只有 session_id
    # 时审计分不出宿主令牌、调试令牌与免鉴权访问，策略也无法区分单聊与群聊。
    user_id: str = ""         # 验签后的 uid / 企微 userid
    auth_method: str = ""     # host_token | debug_token | wecom | anonymous
    chat_type: str = ""       # single | group（企微）；HTTP 为空
    chat_id: str = ""         # 群聊 chatid；文件会发到这个群
    trace_id: str = ""        # 本轮 trace，透传给脚本与下游服务


class ChatMessage(BaseModel):
    """单条对话消息。"""

    role: str
    content: str


class DomainTaskRequest(BaseModel):
    """主 Agent 派发给领域 Agent 的任务协议。"""

    task_id: str
    domain: str
    instruction: str
    customer_scope: CustomerScope
    context_summary: str = ""
    # Prompt Cache 分层：BP2 持久指令、BP3 Checkpoint；context_summary 仅动态尾部
    persistent_instructions: str = ""
    checkpoint_summary: str = ""
    memory_snapshot: dict[str, Any] = Field(default_factory=dict)
    max_rounds: int = 8
    trace_id: str
    # OpenAI 兼容多模态 content parts（如 image_url）；仅首轮用户消息使用
    vision_parts: list[dict[str, Any]] = Field(default_factory=list)


class DomainTaskResult(BaseModel):
    """领域 Agent 返回给主 Agent 的结构化结果。"""

    task_id: str
    domain: str
    status: str
    answer_draft: str | None = None
    structured_data: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)
    confidence: float | None = None
    thought_trace: list[str] = Field(default_factory=list)
