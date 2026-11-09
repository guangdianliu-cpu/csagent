"""QueryAwareContextSelector 单元测试。"""

from csagent.memory.context_selector import QueryAwareContextSelector
from csagent.memory.session_snapshot import ToolCallSnapshot
from csagent.memory.token_counter import TokenCounter


def test_context_selector_tools():
    counter = TokenCounter("gpt-4o")
    selector = QueryAwareContextSelector(counter)

    tools = [
        ToolCallSnapshot(tool_name="fund_query", arguments_summary="000001", result_summary="净值 1.25"),
        ToolCallSnapshot(tool_name="weather_tool", arguments_summary="beijing", result_summary="晴天 25度"),
    ]

    selected_lines = selector.select_tool_history("000001 收益率", tools, token_budget=100)
    assert len(selected_lines) > 0
    assert any("fund_query" in line for line in selected_lines)


def test_context_selector_drops_debug_log_tool_history():
    counter = TokenCounter("gpt-4o")
    selector = QueryAwareContextSelector(counter)
    tools = [
        ToolCallSnapshot(
            tool_name="grep",
            arguments_summary="墨迹阿婆",
            result_summary='data\\llm_bodies\\wecom_BoWen.json:19: "content": "墨迹阿婆"',
        ),
        ToolCallSnapshot(
            tool_name="fund_query",
            arguments_summary="000001",
            result_summary="净值 1.25",
        ),
    ]
    lines = selector.select_tool_history("分类方法论", tools, token_budget=200)
    assert not any("llm_bodies" in x for x in lines)
    assert any("fund_query" in x for x in lines)
