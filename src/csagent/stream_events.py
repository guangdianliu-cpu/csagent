"""流式事件模型。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class AgentEventType(str, Enum):
    THOUGHT = "thought"
    REASONING = "reasoning"
    PROGRESS = "progress"
    DELTA = "delta"
    # 待确认操作。企微渲染成互动卡片，网页渲染成批准/拒绝按钮。
    CHALLENGE = "challenge"
    # 结构化思考事件（ThoughtEvent.to_dict() 的 JSON 文本），预留给需要结构化数据的
    # 消费端；当前各渠道尚未订阅，与现有 THOUGHT（纯文本）并存，互不影响（M1.1）。
    THOUGHT_EVENT = "thought_event"


class AgentStreamEvent(BaseModel):
    event_type: AgentEventType
    content: str
