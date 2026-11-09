"""对话记录（SQLite）：条数、隔离、清理，以及与模型记忆窗口的分工。

这里钉住的核心不变量是**分工**：记录库负责「界面能翻多久」，short_term 负责
「模型记得多少」。两者容量不同、生命周期不同，任何一次改动把它们又搅回一起，
本文件应该先红。
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.agent import Agent
from csagent.guardrail.input_guard import GuardrailVerdict
from csagent.memory.short_term import ShortTermMemory
from csagent.memory.transcript import SqliteTranscriptStore
from csagent.react.models import DomainTaskResult


@pytest.fixture
def store(tmp_path: Path) -> SqliteTranscriptStore:
    return SqliteTranscriptStore(tmp_path / "sub" / "transcripts.db", max_return=100)


def test_creates_db_and_parent_dir(tmp_path: Path) -> None:
    """父目录不存在也要能起来——首次部署时 data/ 下什么都没有。"""
    store = SqliteTranscriptStore(tmp_path / "a" / "b" / "t.db")
    assert store.path.is_file()


def test_append_then_load_in_chronological_order(store: SqliteTranscriptStore) -> None:
    store.append("web:u1", "第一问", "第一答")
    store.append("web:u1", "第二问", "第二答")

    got = [(m.role, m.content) for m in store.load_recent("web:u1")]
    assert got == [
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
        ("assistant", "第二答"),
    ]


def test_sessions_are_isolated(store: SqliteTranscriptStore) -> None:
    store.append("web:zhang3", "我的问题", "我的回答")
    store.append("web:li4", "别人的问题", "别人的回答")

    assert [m.content for m in store.load_recent("web:zhang3")] == ["我的问题", "我的回答"]
    assert store.load_recent("web:unknown") == []


def test_load_recent_returns_the_latest_not_the_earliest(tmp_path: Path) -> None:
    """超出上限时保留的必须是最近的那几条，顺序仍是正序。"""
    store = SqliteTranscriptStore(tmp_path / "t.db", max_return=4)
    for i in range(10):
        store.append("web:u1", f"问{i}", f"答{i}")

    got = [m.content for m in store.load_recent("web:u1")]
    assert got == ["问8", "答8", "问9", "答9"]


def test_limit_cannot_exceed_max_return(tmp_path: Path) -> None:
    """调用方传再大的 limit 也不能突破配置上限，否则一次请求能把整库拖出来。"""
    store = SqliteTranscriptStore(tmp_path / "t.db", max_return=2)
    for i in range(10):
        store.append("web:u1", f"问{i}", f"答{i}")

    assert len(store.load_recent("web:u1", limit=1000)) == 2


def test_long_message_is_clipped_on_write(tmp_path: Path) -> None:
    store = SqliteTranscriptStore(tmp_path / "t.db", max_message_chars=50)
    store.append("web:u1", "短问题", "很长的回答" * 100)

    reply = store.load_recent("web:u1")[1].content
    assert len(reply) < 100
    assert reply.endswith("（已截断）")


def test_empty_side_is_not_recorded(store: SqliteTranscriptStore) -> None:
    """只发附件（文本为空）时不该在界面上留一个空气泡。"""
    store.append("web:u1", "", "只有回答")
    assert [(m.role, m.content) for m in store.load_recent("web:u1")] == [
        ("assistant", "只有回答")
    ]


def test_purge_removes_only_expired_rows(tmp_path: Path) -> None:
    store = SqliteTranscriptStore(tmp_path / "t.db")
    store.append("web:u1", "旧问", "旧答")
    # 直接把这两行改成 40 天前，避免测试里真的等
    import sqlite3

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE messages SET created_at = ?", (time.time() - 40 * 86400,))
    store.append("web:u1", "新问", "新答")

    assert store.purge_older_than(30) == 2
    assert [m.content for m in store.load_recent("web:u1")] == ["新问", "新答"]


def test_purge_disabled_when_days_not_positive(store: SqliteTranscriptStore) -> None:
    store.append("web:u1", "问", "答")
    assert store.purge_older_than(0) == 0
    assert len(store.load_recent("web:u1")) == 2


def test_write_failure_does_not_raise(store: SqliteTranscriptStore, monkeypatch) -> None:
    """落库失败绝不能把用户已经拿到的回复变成一次失败请求。"""
    import sqlite3

    def _boom() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", _boom)
    store.append("web:u1", "问", "答")  # 不抛即通过


# ---------------------------------------------------- 与 Agent / short_term 的分工


class _FakeInputGuard:
    async def review(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(passed=True, violations=[])


class _FakeOutputGuard:
    def review(self, text: str):
        from csagent.guardrail.output_guard import OutputGuardVerdict

        return OutputGuardVerdict(passed=True, sanitized_text=text)


class _FakeSnapshotStore:
    def load(self, session_id: str) -> dict:
        return {}

    def update(self, session_id: str, domain: str, data: dict) -> None:
        return None


def _make_agent(memory: ShortTermMemory, transcript) -> Agent:
    react_loop = MagicMock()
    react_loop.run = AsyncMock(
        return_value=DomainTaskResult(
            task_id="t1",
            domain="generic",
            status="success",
            answer_draft="处理完成：ok",
            confidence=0.9,
            structured_data={},
        )
    )
    agent = Agent(
        react_loop=react_loop,
        tool_registry=MagicMock(),
        short_term_memory=memory,
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        session_snapshot_store=_FakeSnapshotStore(),
        transcript_store=transcript,
        show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_turn_lands_in_both_stores(tmp_path: Path) -> None:
    """一轮对话同时写进记忆窗口和记录库——同一个写入点，两份留存。"""
    memory = ShortTermMemory()
    transcript = SqliteTranscriptStore(tmp_path / "t.db")
    agent = _make_agent(memory, transcript)

    async for _ in agent.handle_message_stream(session_id="s-ok", text="帮我列一下目录"):
        pass

    assert len(memory.load("s-ok")) == 2
    assert [m.content for m in transcript.load_recent("s-ok")] == [
        "帮我列一下目录",
        "处理完成：ok",
    ]


@pytest.mark.asyncio
async def test_transcript_outlives_the_memory_window(tmp_path: Path) -> None:
    """窗口滚出去的轮次，界面还能翻到——这正是引入记录库的理由。"""
    memory = ShortTermMemory(max_messages=4)   # 只记 2 轮
    transcript = SqliteTranscriptStore(tmp_path / "t.db")
    agent = _make_agent(memory, transcript)

    for i in range(5):
        async for _ in agent.handle_message_stream(session_id="s1", text=f"第{i}问"):
            pass

    assert len(memory.load("s1")) == 4                    # 模型记得 2 轮
    assert len(agent.load_history("s1")) == 10            # 界面能翻 5 轮
    assert agent.load_history("s1")[0].content == "第0问"


def test_load_history_falls_back_when_transcript_broken(tmp_path: Path) -> None:
    """记录库读不了时退回记忆窗口：看到的少一些，总好过整块空白。"""
    memory = ShortTermMemory()
    memory.append("s1", "问", "答")
    broken = MagicMock()
    broken.load_recent.side_effect = RuntimeError("db gone")

    agent = _make_agent(memory, broken)
    assert [m.content for m in agent.load_history("s1")] == ["问", "答"]
