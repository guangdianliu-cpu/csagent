"""ReAct 循环中的结构化思考事件（M1.1）。

替代原来 `emit_thought(f"[Round {n}] ...")` 的纯字符串拼接方式：
- 对审计 / 前端调试提供结构化字段（round_no / phase / tool_name / duration_ms）；
- `to_text()` 生成与旧版完全一致（或更丰富）的文本，保证向后兼容。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 参数 / 结果预览的截断上限（避免审计与前端消息无界增长）
ARGUMENTS_PREVIEW_LIMIT = 200
RESULT_PREVIEW_LIMIT = 500

# 合法的事件阶段：
#   llm_call   —— 模型输出的思考文本（非工具调用部分）
#   tool_exec  —— 即将执行某个工具（执行前，无 duration）
#   tool_result—— 工具执行完毕（含 duration_ms，成功或失败）
#   stall      —— 连续相同调用或本轮工具次数用尽
#   reflect    —— 低置信度复核
#   finalize   —— 强制收口
#   timeout    —— 整体超时（round_no=0，系统级）
_VALID_PHASES = frozenset(
    {"llm_call", "tool_exec", "tool_result", "stall", "reflect", "finalize", "timeout"}
)


@dataclass
class ThoughtEvent:
    """ReAct 循环中的单条结构化思考事件。"""

    round_no: int
    phase: str
    tool_name: str | None = None
    arguments_preview: str | None = None
    result_preview: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_text(self) -> str:
        """生成向后兼容的展示文本，格式与旧版 `[Round N] ...` 保持一致。"""
        prefix = f"[Round {self.round_no}]" if self.round_no else "[系统]"
        if self.phase == "tool_exec":
            return f"{prefix} 调用工具: {self.tool_name}({self.arguments_preview or ''})"
        if self.phase == "tool_result":
            suffix = f" (耗时{self.duration_ms}ms)" if self.duration_ms is not None else ""
            body = self.result_preview if self.result_preview is not None else (self.error or "")
            return f"{prefix} 工具结果{suffix}:\n{body}"
        body = self.result_preview if self.result_preview is not None else (self.error or "")
        return f"{prefix} {body}".rstrip()

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict，供审计缓冲 / 前端结构化事件使用。"""
        return {
            "round_no": self.round_no,
            "phase": self.phase,
            "tool_name": self.tool_name,
            "arguments_preview": self.arguments_preview,
            "result_preview": self.result_preview,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "timestamp": self.timestamp,
        }


def truncate_preview(text: str | None, limit: int) -> str | None:
    """截断预览文本；None 原样返回。"""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"
