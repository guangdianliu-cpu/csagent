"""分级记忆 L1/L2（M3.3）测试。"""

from __future__ import annotations

import asyncio

import pytest

from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.memory.graded import GradedMemory, heuristic_turn_summary
from csagent.memory.sqlite_store import SqliteMemoryStore
from csagent.memory.token_counter import TokenCounter


def test_heuristic_keeps_identifier_and_drops_decimals():
    """标识留下，像精确数值的小数必须拿掉。"""
    text = heuristic_turn_summary(
        turn_id=2,
        user_text="查一下 000001",
        assistant_text="000001 最近收益 1.2345，建议再看 artifact",
        token_counter=TokenCounter("gpt-4o"),
        max_tokens=200,
    )
    assert "000001" in text
    assert "1.2345" not in text
    assert "第 2 轮" in text


def test_l1_persists_and_l2_merges_every_n(tmp_path):
    store = SqliteMemoryStore(tmp_path / "csagent.db")
    graded = GradedMemory(
        store,
        llm_client=None,
        token_counter=TokenCounter("gpt-4o"),
        l2_merge_every=3,
        enabled=True,
    )
    handle = store.begin_turn("web:u1")
    assert handle is not None
    for i in range(1, 4):
        store.append_turn(
            "web:u1",
            user_text=f"问题{i} 关于 ALPHA",
            assistant_text=f"答{i}",
            turn=store.begin_turn("web:u1") if i > 1 else handle,
        )
        ep = store.current_episode_id("web:u1")
        graded.schedule_after_turn(
            "web:u1",
            episode_id=ep,
            turn_id=i,
            user_text=f"问题{i} 关于 ALPHA",
            assistant_text=f"答{i}",
        )

    rows = store.list_turn_summaries(store.current_episode_id("web:u1"))
    assert len(rows) == 3
    assert all("ALPHA" in r.summary or "问题" in r.summary for r in rows)

    checkpoint = store.latest_checkpoint(store.current_episode_id("web:u1"))
    assert checkpoint is not None
    assert checkpoint.covers_from == 1
    assert checkpoint.covers_to == 3
    block = graded.format_l2_block(store.current_episode_id("web:u1"))
    assert "Checkpoint" in block


def test_l1_block_skips_recent_window_turns(tmp_path):
    store = SqliteMemoryStore(tmp_path / "csagent.db")
    graded = GradedMemory(store, llm_client=None, l2_merge_every=99)
    ep = "web:u1#0"
    for i in range(1, 6):
        store.upsert_turn_summary(
            episode_id=ep,
            turn_id=i,
            session_id="web:u1",
            summary=f"轮次{i} 做了 ALPHA-{i}",
            tokens=10,
        )
    block = graded.format_l1_block(ep, before_turn=4, token_budget=800)
    assert "轮次1" in block
    assert "轮次3" in block
    assert "轮次4" not in block
    assert "轮次5" not in block


def test_l2_rolling_merge_keeps_latest_and_earliest(tmp_path):
    """超预算时不得把头截断成只留最老一段——最新一批标识必须还在。"""
    store = SqliteMemoryStore(tmp_path / "csagent.db")
    counter = TokenCounter("gpt-4o")
    graded = GradedMemory(
        store,
        llm_client=None,
        token_counter=counter,
        l2_merge_every=2,
        l2_max_tokens=60,
        enabled=True,
    )
    early = "EARLYID " + ("alpha " * 40)
    late = "LATEID " + ("omega " * 40)
    for i in range(1, 5):
        marker = early if i <= 2 else late
        store.upsert_turn_summary(
            episode_id="web:u1#0",
            turn_id=i,
            session_id="web:u1",
            summary=marker,
            tokens=counter.count_text(marker),
        )
        graded._maybe_merge_l2_sync("web:u1#0")

    checkpoint = store.latest_checkpoint("web:u1#0")
    assert checkpoint is not None
    assert checkpoint.covers_from == 1
    assert checkpoint.covers_to == 4
    # 旧实现 truncate(旧+新) 会丢掉 LATEID，Checkpoint 冻在 EARLYID 上。
    assert "LATEID" in checkpoint.summary
    assert "EARLYID" in checkpoint.summary


@pytest.mark.asyncio
async def test_background_l1_does_not_count_on_request_audit_buffer(tmp_path):
    """后台 L1 LLM 不得把用量记进当前请求的审计缓冲（M4.1）。"""

    class _SpyLlm:
        seen_buffer = None

        async def chat(self, messages, **kwargs):
            from csagent.audit.trace_buffer import get_audit_buffer

            self.seen_buffer = get_audit_buffer()
            if self.seen_buffer is not None:
                self.seen_buffer.note_llm_call(prompt_tokens=77, completion_tokens=7)
            return "摘要 KEEP-ID"

    store = SqliteMemoryStore(tmp_path / "csagent.db")
    spy = _SpyLlm()
    graded = GradedMemory(
        store, llm_client=spy, token_counter=TokenCounter("gpt-4o"),
        l2_merge_every=99, enabled=True,
    )
    handle = store.begin_turn("web:u1")
    assert handle is not None
    store.append_turn(
        "web:u1", user_text="问 KEEP-ID", assistant_text="答", turn=handle,
    )

    request_buf = AuditTraceBuffer()
    token = audit_trace_var.set(request_buf)
    try:
        graded.schedule_after_turn(
            "web:u1",
            episode_id=handle.episode_id,
            turn_id=handle.turn_id,
            user_text="问 KEEP-ID",
            assistant_text="答",
        )
        for _ in range(50):
            if store.list_turn_summaries(handle.episode_id):
                break
            await asyncio.sleep(0.02)
    finally:
        audit_trace_var.reset(token)

    assert spy.seen_buffer is None, "后台任务应拿不到本请求的 audit buffer"
    assert request_buf.prompt_tokens == 0
