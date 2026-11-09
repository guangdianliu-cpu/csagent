"""react.stall_guard：连续重复 vs 次数上限。"""

from __future__ import annotations

from csagent.react.models import ActionType, AgentAction
from csagent.react.stall_guard import StallGuard, StallReason


def _call_tool(tool: str, **arguments) -> AgentAction:
    return AgentAction(action=ActionType.CALL_TOOL, tool=tool, arguments=arguments)


def test_consecutive_identical_calls_trigger_stall():
    guard = StallGuard(repeat_threshold=3, max_tool_calls=100)
    action = _call_tool("run_script", script="count_researchers.py")
    assert guard.is_stalled(action) is False
    assert guard.is_stalled(action) is False
    assert guard.is_stalled(action) is True
    assert guard.last_reason == StallReason.REPEAT
    assert "超预算" not in guard.reject_message()
    assert "连续" in guard.reject_message()


def test_intervening_edit_resets_run_script_streak():
    """改脚本再跑同一路径：中间签名不同，不应当空转。

    对应 2026-09-08「谁的研报写得最多」：count_researchers.py 跑过两次
    （失败一次、成功一次），edit_file 后再跑第三次被旧逻辑误杀。
    """
    guard = StallGuard(repeat_threshold=3, max_tool_calls=100)
    run = _call_tool("run_script", script="count_researchers.py")
    assert guard.is_stalled(run) is False
    assert guard.is_stalled(_call_tool("glob", pattern="**/ask.py")) is False
    assert guard.is_stalled(run) is False
    assert guard.is_stalled(_call_tool("edit_file", path="count_researchers.py")) is False
    assert guard.is_stalled(run) is False
    assert guard.last_reason is None


def test_different_arguments_do_not_count_as_repeat():
    guard = StallGuard(repeat_threshold=2, max_tool_calls=100)
    assert guard.is_stalled(_call_tool("t", a=1)) is False
    assert guard.is_stalled(_call_tool("t", a=2)) is False
    assert guard.is_stalled(_call_tool("t", a=3)) is False


def test_exceeding_max_tool_calls_triggers_stall():
    guard = StallGuard(repeat_threshold=100, max_tool_calls=2)
    assert guard.is_stalled(_call_tool("t", a=1)) is False
    assert guard.is_stalled(_call_tool("t", a=2)) is False
    assert guard.is_stalled(_call_tool("t", a=3)) is True
    assert guard.last_reason == StallReason.MAX_TOOL_CALLS
    assert "次数已用尽" in guard.reject_message()
    assert "超预算" not in guard.reject_message()
