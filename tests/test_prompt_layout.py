"""PromptLayoutOptimizer 单元测试。"""

from csagent.memory.prompt_layout import PromptLayoutOptimizer
from csagent.memory.token_counter import TokenCounter
from csagent.react.models import CustomerScope, DomainTaskRequest


def test_prompt_layout_sort_tools():
    counter = TokenCounter("gpt-4o")
    layout = PromptLayoutOptimizer(counter)

    tools = [
        {"type": "function", "function": {"name": "zebra_tool"}},
        {"type": "function", "function": {"name": "alpha_tool"}},
    ]
    sorted_tools = layout.sort_tool_schemas(tools)
    assert sorted_tools[0]["function"]["name"] == "alpha_tool"
    assert sorted_tools[1]["function"]["name"] == "zebra_tool"


def test_prompt_layout_assemble():
    counter = TokenCounter("gpt-4o")
    layout = PromptLayoutOptimizer(counter)

    req = DomainTaskRequest(
        task_id="t1",
        trace_id="tr_1",
        domain="agent",
        instruction="查询华夏成长",
        customer_scope=CustomerScope(session_id="s1"),
    )

    messages = layout.assemble_initial_messages(
        system_prompt="你是投研助手",
        request=req,
        persistent_instructions="必须谨慎推荐",
        checkpoint_summary="- 之前查了 000001",
        dynamic_context="上下文数据",
    )

    assert len(messages) >= 3
    assert messages[0]["role"] == "system"
    assert "必须谨慎推荐" in messages[0]["content"]
    assert "Checkpoint" in messages[1]["content"]
    last_user = messages[-1]["content"]
    assert last_user.startswith("本轮用户问题")
    assert last_user.index("查询华夏成长") < last_user.index("上下文数据")
    # 2026-08-21：customer_id 整条链路已移除，不再往提示词里注入「客户身份已绑定」
    assert "customer_id" not in last_user


def test_checkpoint_is_framed_as_lossy_not_authoritative():
    """Checkpoint 必须自带「可能失真、以工具返回为准」的定性。

    回归 audit 2026-08-16 14:47：某轮压缩把「某分类筛出 1773 条」记成「全量 1773 条」，
    而注入时那句伪造的助手回复写着「已确认结论，将在后续任务中严格遵循」。
    模型于是宁可信摘要也不信工具，连查三轮去证伪一个正确的 matched=1773，
    14 轮往返里废掉 4 轮。摘要是有损产物，不能和实时工具返回平起平坐。
    """
    from csagent.memory.prompt_layout import PromptLayoutOptimizer
    from csagent.react.models import CustomerScope, DomainTaskRequest

    layout = PromptLayoutOptimizer()
    req = DomainTaskRequest(
        task_id="t2",
        trace_id="tr_2",
        domain="agent",
        instruction="继续",
        customer_scope=CustomerScope(session_id="s2"),
    )
    messages = layout.assemble_initial_messages(
        system_prompt="sys",
        request=req,
        checkpoint_summary="- 已查询 active_equity_funds（全量1773条）",
    )

    checkpoint_msg = messages[1]["content"]
    ack_msg = messages[2]["content"]

    # 必须点明是压缩产物、可能失真
    assert "压缩" in checkpoint_msg
    assert ("偏差" in checkpoint_msg) or ("遗漏" in checkpoint_msg)
    # 必须给出冲突时的优先级
    assert "以工具返回为准" in checkpoint_msg
    assert "以工具返回为准" in ack_msg
    # 不得再把它说成不可质疑的既定事实
    assert "严格遵循" not in ack_msg
    assert "已确认结论" not in ack_msg


def test_no_checkpoint_means_no_extra_turns():
    """没有 Checkpoint 时不该凭空插入那两条消息。"""
    from csagent.memory.prompt_layout import PromptLayoutOptimizer
    from csagent.react.models import CustomerScope, DomainTaskRequest

    layout = PromptLayoutOptimizer()
    req = DomainTaskRequest(
        task_id="t3",
        trace_id="tr_3",
        domain="agent",
        instruction="你好",
        customer_scope=CustomerScope(session_id="s3"),
    )
    messages = layout.assemble_initial_messages(
        system_prompt="sys", request=req, checkpoint_summary=""
    )
    assert [m["role"] for m in messages] == ["system", "user"]
