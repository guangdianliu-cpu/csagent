"""react.thought_event.ThoughtEvent 结构化事件测试（M1.1）。"""

from __future__ import annotations

from csagent.react.thought_event import ThoughtEvent, truncate_preview


def test_tool_exec_to_text_matches_legacy_format():
    event = ThoughtEvent(
        round_no=2, phase="tool_exec", tool_name="grep", arguments_preview='{"pattern": "foo"}'
    )
    assert event.to_text() == '[Round 2] 调用工具: grep({"pattern": "foo"})'


def test_tool_result_to_text_includes_duration():
    event = ThoughtEvent(
        round_no=2, phase="tool_result", tool_name="grep", result_preview="找到 3 处", duration_ms=120
    )
    text = event.to_text()
    assert "耗时120ms" in text
    assert "找到 3 处" in text


def test_tool_result_to_text_falls_back_to_error_when_no_result():
    event = ThoughtEvent(
        round_no=1, phase="tool_result", tool_name="http_call", error="连接超时", duration_ms=50
    )
    text = event.to_text()
    assert "连接超时" in text


def test_llm_call_to_text_uses_round_prefix():
    event = ThoughtEvent(round_no=3, phase="llm_call", result_preview="我先查一下持仓")
    assert event.to_text() == "[Round 3] 我先查一下持仓"


def test_system_level_event_uses_system_prefix_when_round_zero():
    event = ThoughtEvent(round_no=0, phase="timeout", result_preview="ReAct 循环超时，强制结束")
    assert event.to_text().startswith("[系统]")


def test_to_dict_contains_all_fields_and_is_json_serializable():
    import json

    event = ThoughtEvent(
        round_no=1,
        phase="tool_exec",
        tool_name="grep",
        arguments_preview="{}",
        result_preview=None,
        duration_ms=None,
        error=None,
    )
    data = event.to_dict()
    for key in (
        "round_no", "phase", "tool_name", "arguments_preview",
        "result_preview", "duration_ms", "error", "timestamp",
    ):
        assert key in data
    json.dumps(data)  # 不应抛异常


def test_truncate_preview_returns_none_for_none_input():
    assert truncate_preview(None, 10) is None


def test_truncate_preview_keeps_short_text_unchanged():
    assert truncate_preview("短文本", 10) == "短文本"


def test_truncate_preview_truncates_long_text_with_ellipsis():
    text = "a" * 20
    truncated = truncate_preview(text, 10)
    assert truncated == "a" * 10 + "…"
    assert len(truncated) == 11
