"""请求级审计缓冲：收集思考过程、结构化事件、工具调用与 LLM 轮次，供 SessionAuditLogger 落盘。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    """单次工具调用的审计记录（M2.2）。"""

    round_no: int
    tool_name: str
    arguments_preview: str
    result_preview: str
    duration_ms: int
    # success | error | rejected（参数非法 / 停滞）| cancelled | denied（权限或策略拒绝）
    # | challenged（等待用户确认，未执行）
    status: str
    # 策略判定：风险等级、判定结果（allow / deny / challenge / approved）与命中的规则 id
    risk: str = ""
    decision: str = ""
    rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditTraceBuffer:
    """单次用户请求的审计缓冲。"""

    thoughts: list[str] = field(default_factory=list)
    # ThoughtEvent.to_dict() 的结构化版本，供审计详情页渲染时间线（M1.1）
    events: list[dict[str, Any]] = field(default_factory=list)
    # 工具调用明细：耗时 / 参数摘要 / 结果摘要 / 状态（M2.2）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # LLM 轮次不再在此留存全文——原文由 data/llm_bodies/ 全量落盘，且比这里多出
    # tools schema、token 用量、请求/响应分离与重试次数。这里只留计数与指向原文的路径。
    llm_turn_count: int = 0
    llm_body_path: str = ""
    # M4.1：本请求所有 LLM 往返的用量合计（API 回报，不是本地 tiktoken 估算）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 本轮注入上下文各层的占用（本地计数），用来对照「钱花在哪一层」
    context_layers: dict[str, Any] = field(default_factory=dict)
    # 本轮对外部状态的改动证据：写了什么文件（sha256）、跑了哪份脚本（sha256 + 存档）、
    # 把什么发给了谁。参数预览只有 200 字，事后靠它还原「到底做了什么」
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    # 本轮生效配置的指纹与策略版本（agent 在本轮开始时登记）
    config_digest: str = ""
    policy_version: str = ""
    # 本轮策略要求用户确认的操作（给卡片用，不进模型记忆）
    challenges: list[dict[str, Any]] = field(default_factory=list)

    def add_thought(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.thoughts.append(text)

    def add_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def add_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record.to_dict())

    def note_llm_call(self, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """记一次 LLM 调用（只计数 + 累加用量，原文在 data/llm_bodies/）。"""
        self.llm_turn_count += 1
        self.prompt_tokens += max(0, int(prompt_tokens or 0))
        self.completion_tokens += max(0, int(completion_tokens or 0))

    def set_context_layers(self, layers: dict[str, Any]) -> None:
        """登记本轮上下文各层占用；覆盖写，组装只发生一次。"""
        self.context_layers = dict(layers)

    def set_llm_body_path(self, body_path: str) -> None:
        """登记本轮原文文件路径；同一 trace 多轮共用一个文件，首次写入即可。"""
        if body_path and not self.llm_body_path:
            self.llm_body_path = body_path


audit_trace_var: ContextVar[AuditTraceBuffer | None] = ContextVar(
    "audit_trace_buffer",
    default=None,
)


def get_audit_buffer() -> AuditTraceBuffer | None:
    return audit_trace_var.get()


def append_audit_thought(text: str) -> None:
    buf = get_audit_buffer()
    if buf is not None:
        buf.add_thought(text)


def append_audit_thought_event(event: dict[str, Any]) -> None:
    buf = get_audit_buffer()
    if buf is not None:
        buf.add_event(event)


def append_audit_tool_call(record: ToolCallRecord) -> None:
    buf = get_audit_buffer()
    if buf is not None:
        buf.add_tool_call(record)


def record_state_change(kind: str, **fields: Any) -> None:
    """登记一条状态改动证据（写文件 / 跑脚本 / 发文件 / 改偏好）。

    工具不自己写审计文件；有副作用的工具经它往本轮缓冲里登记，由 agent 统一落盘。
    没有请求上下文（单测直接调工具）时静默跳过。
    """
    buf = get_audit_buffer()
    if buf is not None:
        buf.state_changes.append({"kind": kind, **fields})


def append_audit_challenge(challenge: dict[str, Any]) -> None:
    """登记本轮的待确认操作。同一张卡片（task_id 相同）只登记一次：模型在一轮里
    重试同一调用时，确认簿复用了同一条，这里不去重的话企微会连发两张一样的卡片。"""
    buf = get_audit_buffer()
    if buf is None:
        return
    task_id = challenge.get("task_id")
    if task_id and any(item.get("task_id") == task_id for item in buf.challenges):
        return
    buf.challenges.append(challenge)
