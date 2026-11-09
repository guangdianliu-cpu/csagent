"""ReAct 循环上下文：状态机 + 运行时数据容器。

所有循环内的可变状态集中在 ReactContext 上，ReactLoop 退化为状态调度器。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from csagent.react.models import DomainTaskRequest, DomainTaskResult
from csagent.react.stall_guard import StallGuard
from csagent.react.thought_event import ThoughtEvent

ThoughtCallback = Callable[[str], Awaitable[None]]
DeltaCallback = Callable[[str], Awaitable[None]]
ThoughtEventCallback = Callable[[ThoughtEvent], Awaitable[None]]


class ReactPhase(str, Enum):
    """ReAct 循环的显式状态。"""

    INIT = "init"
    LLM_CALL = "llm_call"
    PARSE_RESPONSE = "parse_response"
    EXECUTE_TOOLS = "execute_tools"
    CHECK_TERMINATION = "check_termination"
    FORCE_FINALIZE = "force_finalize"
    DONE = "done"


class TerminationReason(str, Enum):
    """循环终止原因（用于日志/审计）。"""

    PLAIN_TEXT_ANSWER = "plain_text_answer"
    TIMEOUT = "timeout"
    STALL = "stall"
    MAX_ROUNDS = "max_rounds"
    LLM_UNAVAILABLE = "llm_unavailable"
    FORCE_FINALIZED = "force_finalized"
    MAX_INVALID = "max_invalid"


@dataclass
class ReactContext:
    """ReAct 循环的运行时上下文，生命周期 = 一次 run() 调用。

    集中管理所有循环内状态，消除 _react_loop_body 的局部变量散落。
    """

    # ---- 请求不可变参数 ----
    request: DomainTaskRequest
    system_prompt: str
    agent_name: str
    max_rounds: int

    # ---- 回调 ----
    emit_event: ThoughtEventCallback
    on_thought: ThoughtCallback | None = None
    on_progress: ThoughtCallback | None = None
    on_delta: DeltaCallback | None = None
    on_reasoning: ThoughtCallback | None = None

    # ---- 循环可变状态 ----
    phase: ReactPhase = ReactPhase.INIT
    round_no: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 头部不可压缩的消息条数（system + Checkpoint 锚点 + 本轮问题），
    # 由 PromptLayoutOptimizer.initial_head_count 给出，循环内压缩据此划保护区
    protected_head: int = 1
    thought_trace: list[str] = field(default_factory=list)
    stall_guard: StallGuard | None = None
    consecutive_invalid: int = 0
    termination_reason: TerminationReason | None = None

    # ---- 工具 schema（全量常驻）----
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)

    # ---- 本轮 LLM 响应（每轮更新）----
    assistant_message: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    content_text: str = ""

    # ---- 结果 ----
    result: DomainTaskResult | None = None

    @property
    def is_done(self) -> bool:
        return self.phase == ReactPhase.DONE
