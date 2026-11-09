"""最近窗口的裁剪语义与 Tier1 预算接线。

背景（audit wecom_BoWen req#108）：上一轮助手回复列出了 11 只基金，用户接着说
「把这 11 只导出 excel」。但最近窗口按固定 250 token 一刀切，清单正好被截在表头
之后，模型手里一只代码都没有，只能把整个分析重跑一遍。

同时 ContextBudgetAllocator 算出的 Tier1 预算（recent_history_tokens）此前无人消费，
1M 窗口只用了 3.8%，却仍按固定小常数裁剪。
"""

from __future__ import annotations

import pytest

from csagent.memory.compactor import CompactorConfig, StructuredCompactor
from csagent.memory.session_snapshot import (
    SessionSnapshotStore,
    ToolCallSnapshot,
    format_tool_digest,
)
from csagent.memory.token_counter import TokenCounter
from csagent.react.models import ChatMessage


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _compactor(**cfg) -> StructuredCompactor:
    return StructuredCompactor(
        llm_client=None,
        token_counter=TokenCounter(),
        config=CompactorConfig(**cfg),
        enabled=False,
    )


# ---------------------------------------------------------------- 末条完整保留


def test_latest_message_is_never_truncated_by_role_cap():
    """末条是指代消解的唯一来源，不受角色上限约束。"""
    listing = "".join(f"| {i} | 00{i:04d}.OF | 某某债券A | 评分 7.{i} |\n" for i in range(40))
    c = _compactor(recent_assistant_max_tokens=50)

    out = c._format_messages([_msg("user", "筛一下"), _msg("assistant", listing)])

    assert "000039.OF" in out, "末条被截断了，清单尾部丢失"
    assert listing.strip() in out


def test_older_messages_still_respect_role_caps():
    c = _compactor(recent_assistant_max_tokens=20, recent_user_max_tokens=20)
    older_answer = "废话 " * 400
    out = c._format_messages(
        [_msg("assistant", older_answer), _msg("user", "接着说")]
    )
    lines = out.splitlines()
    # 第一条被裁，最后一条完整
    assert len(lines[0]) < len(f"assistant: {older_answer}")
    assert lines[-1] == "user: 接着说"


def test_single_message_window_is_kept_whole():
    c = _compactor(recent_assistant_max_tokens=10)
    body = "结论 " * 300
    out = c._format_messages([_msg("assistant", body)])
    assert out == f"assistant: {body}"


def test_latest_has_a_defensive_ceiling():
    """完整保留不等于无上限——异常超长输出不能把窗口吃光。"""
    c = _compactor(recent_latest_max_tokens=30)
    out = c._format_messages([_msg("assistant", "超长 " * 2000)])
    assert TokenCounter().count_text(out) <= 60


# ---------------------------------------------------------------- Tier1 预算


def test_budget_widens_older_message_caps():
    """窗口有余量时，非末条的上限应超过角色基准，而不是恒等于它。"""
    older = "细节 " * 500
    c = _compactor(recent_assistant_max_tokens=30, recent_user_max_tokens=30)
    counter = TokenCounter()

    tight = c._format_messages([_msg("assistant", older), _msg("user", "继续")])
    generous = c._format_messages(
        [_msg("assistant", older), _msg("user", "继续")], token_budget=4000
    )
    assert counter.count_text(generous) > counter.count_text(tight)


def test_budget_shrinks_older_when_latest_is_huge():
    """预算被末条吃掉时，较早消息收敛到下限，但不会整条消失。"""
    c = _compactor(recent_assistant_max_tokens=500, recent_min_tokens=5)
    out = c._format_messages(
        [_msg("user", "背景 " * 300), _msg("assistant", "正文 " * 400)],
        token_budget=60,
    )
    first = out.splitlines()[0]
    assert first.startswith("user: ")
    assert len(first) > len("user: ")  # 没有被抹成空
    assert "正文" in out.splitlines()[-1]


def test_build_context_passes_recent_budget_through():
    c = _compactor(recent_assistant_max_tokens=20)
    history = [_msg("user", "q1"), _msg("assistant", "细节 " * 400)]
    counter = TokenCounter()

    small = c.build_context("s", history, recent_token_budget=None)
    large = c.build_context("s", history, recent_token_budget=5000)
    assert counter.count_text(large.recent_block) >= counter.count_text(small.recent_block)


# ---------------------------------------------------------------- 工具流水进 Checkpoint


def test_tool_digest_renders_calls_including_failures():
    store = SessionSnapshotStore()
    store.append_tool_calls(
        "s1",
        [
            ToolCallSnapshot(
                tool_name="run_script",
                arguments_summary="analyze_v3.py",
                result_summary="严格口径命中 11 只",
            ),
            ToolCallSnapshot(
                tool_name="send_file",
                arguments_summary="优选二级债基_11只.xlsx",
                result_summary="已加入出站队列",
            ),
            # 失败项同样是下一轮该知道的状态，不该被过滤
            ToolCallSnapshot(
                tool_name="run_script",
                arguments_summary="bad.py",
                result_summary="错误: 文件不存在",
            ),
        ],
    )
    digest = format_tool_digest(store, "s1")
    assert "run_script" in digest
    assert "严格口径命中 11 只" in digest
    assert "send_file" in digest
    assert "错误: 文件不存在" in digest


def test_tool_digest_empty_for_unknown_session_and_minimal_store():
    assert format_tool_digest(SessionSnapshotStore(), "nope") == ""

    class _Minimal:
        """没有 load_tool_calls 的极简替身。"""

    assert format_tool_digest(_Minimal(), "s") == ""


@pytest.mark.asyncio
async def test_checkpoint_prompt_includes_tool_digest():
    """Checkpoint 模板要求写「已完成操作」，输入里必须真的有工具流水。"""
    captured: list[list[dict]] = []

    class _Llm:
        async def chat(self, messages, **kwargs):
            captured.append(messages)
            return "### 关键决策与已完成操作\n- 已导出并发送 Excel"

    c = StructuredCompactor(
        llm_client=_Llm(),
        token_counter=TokenCounter(),
        config=CompactorConfig(window=1),
        enabled=True,
        tool_digest_provider=lambda sid: "- send_file(11只.xlsx) → 已加入出站队列",
    )
    history = [_msg("user", "导出"), _msg("assistant", "好的"), _msg("user", "再发一下")]
    await c.refresh("s1", history)

    assert captured, "未发起压缩调用"
    user_msg = captured[0][1]["content"]
    assert "已执行的工具调用" in user_msg
    assert "send_file(11只.xlsx)" in user_msg


@pytest.mark.asyncio
async def test_broken_tool_digest_provider_does_not_break_refresh():
    def _boom(_sid: str) -> str:
        raise RuntimeError("snapshot store down")

    class _Llm:
        async def chat(self, messages, **kwargs):
            return "### 用户目标与状态\n- ok"

    c = StructuredCompactor(
        llm_client=_Llm(),
        token_counter=TokenCounter(),
        config=CompactorConfig(window=1),
        enabled=True,
        tool_digest_provider=_boom,
    )
    await c.refresh("s1", [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")])
    assert c.peek_cached_summary("s1")
