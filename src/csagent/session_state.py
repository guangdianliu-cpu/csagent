"""会话 ContextVar：进度 / reasoning 回调。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from csagent.react.models import DomainTaskResult

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass
class SessionState:
    """单次用户消息处理中的回调与可选结果容器。"""

    domain_results: list[DomainTaskResult] = field(default_factory=list)
    context_summary: str = ""
    trace_id: str = ""
    on_progress: ProgressCallback | None = None
    on_reasoning: ProgressCallback | None = None


session_state_var: ContextVar[SessionState | None] = ContextVar(
    "session_state",
    default=None,
)
