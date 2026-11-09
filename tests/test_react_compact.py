"""循环内历史压缩：保留原问题、禁止摘要套娃、保护区由布局器显式给出。

压缩逻辑已从 ReactLoop 移到 memory/compaction_policy.py，与 Agent 层共用同一
水位线（内核审视报告问题 E）。
"""

from csagent.memory.compaction_policy import REACT_COMPACT_MARK, HistoryCompactionPolicy
from csagent.memory.prompt_layout import PromptLayoutOptimizer


def _policy(token_counter=None) -> HistoryCompactionPolicy:
    return HistoryCompactionPolicy(token_counter)


def _msgs(n_extra: int) -> list[dict]:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "本轮任务：查分类方法论"},
    ]
    for i in range(n_extra):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": f"tool_{i}"}}
        ]})
        messages.append({"role": "tool", "content": f"result-{i}"})
    return messages


def _compact_users(messages: list[dict]) -> list[dict]:
    return [
        m for m in messages
        if m.get("role") == "user" and REACT_COMPACT_MARK in str(m.get("content") or "")
    ]


def test_short_react_does_not_compact():
    policy = _policy()
    messages = _msgs(10)
    before = list(messages)
    assert policy.compact_loop_messages(messages, protected_head=2) is False
    assert messages == before
    assert messages[1]["content"] == "本轮任务：查分类方法论"


def test_long_react_keeps_original_user_and_does_not_nest_summary():
    policy = _policy()
    messages = _msgs(40)
    assert policy.compact_loop_messages(messages, protected_head=2) is True
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "本轮任务：查分类方法论"
    assert len(_compact_users(messages)) == 1
    assert _compact_users(messages)[0]["content"].count(REACT_COMPACT_MARK) == 1

    # 再压一次：仍只有一层标记
    for i in range(40):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": f"more_{i}"}}
        ]})
        messages.append({"role": "tool", "content": "x"})
    policy.compact_loop_messages(messages, protected_head=2)
    assert messages[1]["content"] == "本轮任务：查分类方法论"
    assert len(_compact_users(messages)) == 1
    assert _compact_users(messages)[0]["content"].count(REACT_COMPACT_MARK) == 1


def test_protected_head_covers_checkpoint_anchor():
    """带 Checkpoint 时保护区为 4 条，压缩不得动到 Checkpoint 与本轮问题。"""
    layout = PromptLayoutOptimizer()
    protected = layout.initial_head_count(checkpoint_summary="早期摘要")
    assert protected == 4
    assert layout.initial_head_count(checkpoint_summary="") == 2

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[早期对话历史 Checkpoint] 摘要正文"},
        {"role": "assistant", "content": "已了解上述早期对话概要。"},
        {"role": "user", "content": "本轮任务：查分类方法论"},
    ]
    for i in range(40):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": f"tool_{i}"}}
        ]})
        messages.append({"role": "tool", "content": f"result-{i}"})

    assert _policy().compact_loop_messages(messages, protected_head=protected) is True
    assert messages[0]["role"] == "system"
    assert "Checkpoint" in messages[1]["content"]
    assert messages[2]["role"] == "assistant"
    assert messages[3]["content"] == "本轮任务：查分类方法论"


def test_token_counter_present_ignores_message_count_heuristic():
    """有 TokenCounter 时只认 Token 水位：条数再多、Token 不超也不压。

    改造前条数规则会无视 Token 余量抢先触发，正是 Agent 层与循环层「没有协调
    机制」的表现。
    """

    class _FatBudgetCounter:
        model_context_limit = 1_000_000

        def count_messages(self, messages):
            return 100

        def truncate_to_tokens(self, text, n):
            return text

    messages = _msgs(40)
    before = list(messages)
    policy = _policy(_FatBudgetCounter())
    assert policy.compact_loop_messages(messages, protected_head=2) is False
    assert messages == before

    # 水位线压低后立刻触发
    class _TightCounter(_FatBudgetCounter):
        model_context_limit = 100

    assert _policy(_TightCounter()).compact_loop_messages(
        messages, protected_head=2
    ) is True
    assert len(_compact_users(messages)) == 1


# ---------------------------------------------------------------------------
# LLM 摘要版：成功时用摘要替换中间轮次，失败时回落机械截断
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_async_compact_uses_llm_summary_and_keeps_head():
    messages = _msgs(40)
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "### 已完成的步骤与结论\n- 调了 tool_0..tool_n，artifact_id=a1"

    assert await _policy().compact_loop_messages_async(
        messages, protected_head=2, summarize=summarize
    ) is True
    assert messages[1]["content"] == "本轮任务：查分类方法论"
    compacted = _compact_users(messages)
    assert len(compacted) == 1
    assert "artifact_id=a1" in compacted[0]["content"]
    # 交给摘要模型的流水里有工具名与结果原文，不是 60 token 的碎片
    assert "tool_0" in seen[0] and "result-0" in seen[0]


@pytest.mark.asyncio
async def test_async_compact_falls_back_when_llm_fails():
    messages = _msgs(40)

    async def summarize(text: str) -> None:
        return None

    assert await _policy().compact_loop_messages_async(
        messages, protected_head=2, summarize=summarize
    ) is True
    compacted = _compact_users(messages)
    assert len(compacted) == 1
    assert "调用工具" in compacted[0]["content"]


@pytest.mark.asyncio
async def test_async_compact_below_watermark_does_not_call_llm():
    messages = _msgs(2)
    called = False

    async def summarize(text: str) -> str:
        nonlocal called
        called = True
        return "x"

    assert await _policy().compact_loop_messages_async(
        messages, protected_head=2, summarize=summarize
    ) is False
    assert called is False
