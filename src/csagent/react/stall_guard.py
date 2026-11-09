"""ReAct 停滞检测与调用次数上限。

拦的是空转，不是 Token 窗口：

- **连续相同签名**达到阈值：同一工具 + 同一参数连打 N 次（中间没有别的工具）。
  改完脚本再 ``run_script`` 同一路径，中间夹了 ``edit_file``，签名链被打断，不算停滞。
- **本轮工具总次数**超过 ``max_tool_calls``：才是次数预算用尽。

两种原因必须分开写给模型，禁止再合成「停滞或超预算」——模型会原样抄给用户。
"""

from __future__ import annotations

import json
from enum import Enum

from csagent.react.models import AgentAction, ActionType


class StallReason(str, Enum):
    """最近一次 ``is_stalled`` 为真时的原因。"""

    REPEAT = "repeat"
    MAX_TOOL_CALLS = "max_tool_calls"


_REPEAT_REJECT = "连续用相同参数调用同一工具，已取消。改完文件或换参数后再跑，不要原样重试。"
_REPEAT_CANCEL = "已取消：连续相同调用，准备根据已有结果收口。"
_REPEAT_PREVIEW = "连续相同工具调用，强制收口"

_MAX_REJECT = "本轮工具调用次数已用尽，已取消。"
_MAX_CANCEL = "已取消：本轮工具次数已用尽，准备根据已有结果收口。"
_MAX_PREVIEW = "本轮工具调用次数已用尽，强制收口"


class StallGuard:
    """连续重复调用与本轮调用次数上限。"""

    def __init__(self, repeat_threshold: int = 3, max_tool_calls: int = 15) -> None:
        self._repeat_threshold = max(2, int(repeat_threshold))
        self._max_tool_calls = max(1, int(max_tool_calls))
        self._last_signature: str | None = None
        self._repeat_streak = 0
        self.total_tool_calls = 0
        self.last_reason: StallReason | None = None

    @property
    def max_tool_calls(self) -> int:
        return self._max_tool_calls

    def is_stalled(self, action: AgentAction) -> bool:
        """当前动作是否应取消并收口。为真时 ``last_reason`` 有值。"""
        self.last_reason = None
        if action.action != ActionType.CALL_TOOL:
            return False

        self.total_tool_calls += 1
        if self.total_tool_calls > self._max_tool_calls:
            self.last_reason = StallReason.MAX_TOOL_CALLS
            return True

        signature = self._tool_signature(action.tool, action.arguments)
        if signature == self._last_signature:
            self._repeat_streak += 1
        else:
            self._last_signature = signature
            self._repeat_streak = 1

        if self._repeat_streak >= self._repeat_threshold:
            self.last_reason = StallReason.REPEAT
            return True
        return False

    def reject_message(self) -> str:
        """写进被拦那一次 tool 结果，给模型看。"""
        if self.last_reason == StallReason.MAX_TOOL_CALLS:
            return _MAX_REJECT
        return _REPEAT_REJECT

    def cancel_message(self) -> str:
        """同批尚未执行的 tool_call 回填文案。"""
        if self.last_reason == StallReason.MAX_TOOL_CALLS:
            return _MAX_CANCEL
        return _REPEAT_CANCEL

    def preview_message(self) -> str:
        """审计 / thought_event 短句。"""
        if self.last_reason == StallReason.MAX_TOOL_CALLS:
            return _MAX_PREVIEW
        return _REPEAT_PREVIEW

    @staticmethod
    def _tool_signature(tool: str | None, arguments: dict) -> str:
        return json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True)
