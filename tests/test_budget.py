"""ContextBudgetAllocator 单元测试。"""

from csagent.memory.budget import ContextBudgetAllocator, ContextBudgetConfig, BudgetAllocation
from csagent.memory.token_counter import TokenCounter
from csagent.react.models import ChatMessage


def test_budget_allocator_basic():
    counter = TokenCounter("gpt-4o")
    allocator = ContextBudgetAllocator(counter)

    history = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，请问有什么可以帮您？"),
    ]

    alloc = allocator.allocate(
        system_prompt="你是助手",
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
        recent_history=history,
    )

    assert alloc.total_budget > 0
    assert alloc.system_prompt_tokens > 0
    assert alloc.recent_history_tokens > 0
    assert not alloc.needs_compact
    # 即使当前无摘要文本，也应预留 Checkpoint 上限供后续截断
    assert alloc.summary_tokens > 0


def test_tool_history_budget_is_a_fixed_quota():
    """工具流水拿的是固定配额，不随对话多少浮动。

    回归 M0.2 之前的缺陷：它曾从「领域快照」预算派生（``snapshots_tokens // 4 or 80``），
    而快照层供给恒为空，导致这份预算被永久钉死在 80 token、只放得下约 2 条调用。
    """
    counter = TokenCounter("gpt-4o")
    allocator = ContextBudgetAllocator(counter, ContextBudgetConfig(tool_history_tokens=600))

    empty = allocator.allocate(system_prompt="你是助手", tools=[], recent_history=[])
    busy = allocator.allocate(
        system_prompt="你是助手",
        tools=[],
        recent_history=[ChatMessage(role="user", content="细节 " * 500)] * 10,
    )

    assert empty.tool_history_tokens == 600
    assert busy.tool_history_tokens == 600


def test_tool_history_quota_clamped_when_window_is_tiny():
    """可分配空间小于配额时，工具流水最多拿到剩下的全部，不会出现负预算。"""
    counter = TokenCounter("gpt-4o")
    cfg = ContextBudgetConfig(tool_history_tokens=10_000_000)
    allocator = ContextBudgetAllocator(counter, cfg)

    alloc = allocator.allocate(system_prompt="你是助手", tools=[], recent_history=[])

    assert 0 <= alloc.tool_history_tokens <= alloc.available_for_content
    assert alloc.summary_tokens >= 0


def test_budget_allocator_overflow_detection():
    counter = TokenCounter("gpt-4o")
    cfg = ContextBudgetConfig(compact_threshold=0.01)  # 设为极低阈值以测试触发
    allocator = ContextBudgetAllocator(counter, cfg)

    alloc = allocator.allocate(
        system_prompt="非常长" * 1000,
        tools=[],
        recent_history=[],
    )
    assert alloc.needs_compact or allocator.should_compact(100_000)


def test_budget_allocation_as_audit_dict_uses_stable_keys():
    """审计 JSONL 要能按层对照「钱花在哪」；键名改了脚本就对不上。"""
    alloc = BudgetAllocation(
        system_prompt_tokens=1,
        tools_tokens=2,
        recent_history_tokens=3,
        tool_history_tokens=4,
        summary_tokens=5,
        response_reserve=6,
        total_budget=7,
        needs_compact=True,
    )
    dumped = alloc.as_audit_dict()
    assert dumped["system"] == 1
    assert dumped["tools"] == 2
    assert dumped["recent"] == 3
    assert dumped["tool_history"] == 4
    assert dumped["summary_budget"] == 5
    assert dumped["reserve"] == 6
    assert dumped["total_budget"] == 7
    assert dumped["needs_compact"] is True
