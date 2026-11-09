"""记忆回写路径：成功收口写回，客户端断连也要把已流出的片段落盘。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.agent import Agent
from csagent.guardrail.input_guard import GuardrailVerdict
from csagent.memory.short_term import ShortTermMemory
from csagent.react.models import DomainTaskResult
from csagent.stream_events import AgentEventType


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


@pytest.mark.asyncio
async def test_success_appends_short_term():
    """Agent 成功处理消息后应写回短期记忆。"""
    memory = ShortTermMemory()

    # 用 mock ReactLoop，返回固定结果
    mock_react_loop = MagicMock()
    mock_react_loop.run = AsyncMock(return_value=DomainTaskResult(
        task_id="t1", domain="generic", status="success",
        answer_draft="处理完成：ok", confidence=0.9, structured_data={},
    ))

    agent = Agent(
        react_loop=mock_react_loop,
        tool_registry=MagicMock(),
        short_term_memory=memory,
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        session_snapshot_store=_FakeSnapshotStore(),
        show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]

    async for _ in agent.handle_message_stream(
        session_id="s-ok",
        text="帮我列一下目录",
    ):
        pass

    history = memory.load("s-ok")
    assert len(history) == 2
    assert history[0].content == "帮我列一下目录"
    assert "处理完成" in history[1].content


# ---------- 中断路径（M0.1）----------


def _build_agent(memory: ShortTermMemory, react_run) -> Agent:
    """组装一个只替换了 ReactLoop.run 的最小 Agent，供中断用例复用。"""
    mock_react_loop = MagicMock()
    mock_react_loop.run = react_run

    agent = Agent(
        react_loop=mock_react_loop,
        tool_registry=MagicMock(),
        short_term_memory=memory,
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        session_snapshot_store=_FakeSnapshotStore(),
        show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]
    return agent


async def _consume_until_first_delta(stream) -> None:
    """消费到第一个 DELTA 事件就停手，模拟用户在回答说到一半时关掉页面。"""
    async for event in stream:
        if event.event_type == AgentEventType.DELTA:
            return


@pytest.mark.asyncio
async def test_interrupted_stream_persists_partial_reply():
    """断连时已流出的片段应写回记忆，并带上「未完成」标记。

    回归的是「用户屏幕上留着半截答案、模型侧零记录」——下一轮模型会否认自己说过话。
    """
    memory = ShortTermMemory()
    never_finishes = asyncio.Event()

    async def _stalled_run(*_args, **kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            await on_delta("已经说了前半段")
        # 吐完半句后一直挂着，等测试侧主动关闭生成器
        await never_finishes.wait()
        return DomainTaskResult(
            task_id="t-cut", domain="generic", status="success",
            answer_draft="完整回答", confidence=0.9, structured_data={},
        )

    agent = _build_agent(memory, _stalled_run)

    stream = agent.handle_message_stream(session_id="s-cut", text="帮我查一下这只基金")
    await _consume_until_first_delta(stream)
    await stream.aclose()

    history = memory.load("s-cut")
    assert len(history) == 2, "断连轮次必须留下一问一答，否则模型下一轮会失忆"
    assert history[0].content == "帮我查一下这只基金"
    assert "已经说了前半段" in history[1].content
    assert "未完成" in history[1].content, "半截回答必须标记出来，避免被当成完整应答"


@pytest.mark.asyncio
async def test_interrupted_before_any_output_writes_nothing():
    """一个字都没吐出去就断开：用户屏幕是空的，记忆里也不该留半条对话。"""
    memory = ShortTermMemory()
    never_finishes = asyncio.Event()

    async def _silent_run(*_args, **kwargs):
        await never_finishes.wait()
        return DomainTaskResult(
            task_id="t-silent", domain="generic", status="success",
            answer_draft="完整回答", confidence=0.9, structured_data={},
        )

    agent = _build_agent(memory, _silent_run)

    stream = agent.handle_message_stream(session_id="s-silent", text="帮我查一下这只基金")
    # 只取一个事件（必然是进度事件），此时 ReAct 还没产出任何正文
    async for _ in stream:
        break
    await stream.aclose()

    assert memory.load("s-silent") == []


@pytest.mark.asyncio
async def test_user_stop_marks_reply_as_stopped():
    """用户点「停止」：半截回答带「用户已手动停止」标记，而不是「连接中断」。"""
    memory = ShortTermMemory()
    never_finishes = asyncio.Event()

    async def _stalled_run(*_args, **kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            await on_delta("已经说了前半段")
        await never_finishes.wait()

    agent = _build_agent(memory, _stalled_run)
    stream = agent.handle_message_stream(session_id="s-stop", text="长任务")
    await _consume_until_first_delta(stream)
    agent.mark_stop_requested("s-stop")
    await stream.aclose()

    history = memory.load("s-stop")
    assert len(history) == 2
    assert "已经说了前半段" in history[1].content
    assert "手动停止" in history[1].content
    assert "连接中断" not in history[1].content


@pytest.mark.asyncio
async def test_user_stop_before_output_still_records_turn():
    """停在任何正文产出之前：也要留下一问一答，下一句「继续」才有指代。"""
    memory = ShortTermMemory()
    never_finishes = asyncio.Event()

    async def _silent_run(*_args, **kwargs):
        await never_finishes.wait()

    agent = _build_agent(memory, _silent_run)
    stream = agent.handle_message_stream(session_id="s-stop2", text="长任务")
    async for _ in stream:
        break
    agent.mark_stop_requested("s-stop2")
    await stream.aclose()

    history = memory.load("s-stop2")
    assert len(history) == 2
    assert history[0].content == "长任务"
    assert "手动停止" in history[1].content


# ---------- 统一持久化存储（M1）----------


def _sqlite_agent(db_path, react_run) -> Agent:
    """记忆与对话记录接同一个 SqliteMemoryStore，等价于 sqlite 后端的生产接线。"""
    from csagent.memory.sqlite_store import SqliteMemoryStore

    store = SqliteMemoryStore(db_path)
    mock_react_loop = MagicMock()
    mock_react_loop.run = react_run

    agent = Agent(
        react_loop=mock_react_loop,
        tool_registry=MagicMock(),
        short_term_memory=store,
        transcript_store=store,
        session_snapshot_store=store,
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]
    return agent


def _ok_run(answer: str):
    return AsyncMock(return_value=DomainTaskResult(
        task_id="t1", domain="generic", status="success",
        answer_draft=answer, confidence=0.9, structured_data={},
    ))


@pytest.mark.asyncio
async def test_agent_memory_survives_restart(tmp_path):
    """重启后模型仍记得上一轮——M1 的核心主张，在 Agent 这一层验证。"""
    db = tmp_path / "csagent.db"

    agent = _sqlite_agent(db, _ok_run("上一轮的结论是 A"))
    async for _ in agent.handle_message_stream(session_id="s-p", text="帮我分析一下"):
        pass

    reborn = _sqlite_agent(db, _ok_run("无所谓"))  # 新进程
    history = reborn._short_term_memory.load("s-p")

    assert [m.content for m in history] == ["帮我分析一下", "上一轮的结论是 A"]


@pytest.mark.asyncio
async def test_unified_store_writes_each_turn_exactly_once(tmp_path):
    """记忆与记录是同一个 store 时，一轮只能落一次，不能被写两遍。"""
    import sqlite3
    from contextlib import closing

    db = tmp_path / "csagent.db"
    agent = _sqlite_agent(db, _ok_run("好的"))
    async for _ in agent.handle_message_stream(session_id="s-once", text="你好"):
        pass

    with closing(sqlite3.connect(db)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's-once'"
        ).fetchone()[0]

    assert count == 2, "一轮 = user + assistant 两行；4 行说明记忆和记录各写了一遍"


@pytest.mark.asyncio
async def test_unified_store_keeps_model_and_ui_text_apart(tmp_path):
    """合并存储不能把富文本糊进界面，也不能让模型丢掉附件路径。"""
    db = tmp_path / "csagent.db"
    agent = _sqlite_agent(db, _ok_run("已处理"))
    rich = "分析一下\n[附件] 报表.xlsx\n路径: data/uploads/s-t/报表.xlsx"

    async for _ in agent.handle_message_stream(
        session_id="s-t", text=rich, transcript_user_text="分析一下 [附件] 报表.xlsx"
    ):
        pass

    model_side = agent._short_term_memory.load("s-t")
    ui_side = agent.load_history("s-t")

    assert "路径: data/uploads" in model_side[0].content
    assert "路径: data/uploads" not in ui_side[0].content


@pytest.mark.asyncio
async def test_interrupted_turn_is_flagged_in_unified_store(tmp_path):
    """断连轮次落库时要带上 interrupted 位，事后才能解释回答为何截断。"""
    import sqlite3
    from contextlib import closing

    db = tmp_path / "csagent.db"
    never_finishes = asyncio.Event()

    async def _stalled_run(*_args, **kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            await on_delta("说了一半")
        await never_finishes.wait()

    agent = _sqlite_agent(db, _stalled_run)
    stream = agent.handle_message_stream(session_id="s-cut", text="帮我查")
    await _consume_until_first_delta(stream)
    await stream.aclose()

    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT role, interrupted FROM messages WHERE session_id = 's-cut' ORDER BY id"
        ).fetchall()

    assert rows == [("user", 0), ("assistant", 1)]


# ---------------------------------------------------------------- 并发定序（M2.2）


@pytest.mark.asyncio
async def test_parallel_turns_are_ordered_by_arrival_not_by_completion(tmp_path):
    """同一个人并发提两个问题，历史要按「谁先问」排，不是「谁先跑完」。

    这是 M2.2 在 Agent 这一层的验收点：先到的那轮故意跑得慢，若 turn_id 留到回写时
    才分配，它就会被排到后面去，用户翻历史会看到答非所问。
    """
    db = tmp_path / "csagent.db"
    fast_done = asyncio.Event()

    async def _run(request, **kwargs):
        # 靠用户输入区分两路，避免依赖调用顺序。
        # 慢的那路在快的跑完后**再多等一截**：Agent 的流式多路复用按 0.02s 轮询，
        # 只差一个拍子的话两轮谁先落库是随机的，那样这条测试就成了掷骰子。
        if "慢" in request.instruction:
            await fast_done.wait()
            await asyncio.sleep(0.2)
            answer = "慢答"
        else:
            answer = "快答"
            fast_done.set()
        return DomainTaskResult(
            task_id="t", domain="generic", status="success",
            answer_draft=answer, confidence=0.9, structured_data={},
        )

    agent = _sqlite_agent(db, _run)

    async def _ask(text: str) -> None:
        async for _ in agent.handle_message_stream(session_id="s-par", text=text):
            pass

    slow = asyncio.create_task(_ask("慢问题"))
    await asyncio.sleep(0.05)          # 保证「慢问题」先到达并拿到号
    fast = asyncio.create_task(_ask("快问题"))
    await asyncio.gather(slow, fast)

    history = [m.content for m in agent._short_term_memory.load("s-par")]

    assert history == ["慢问题", "慢答", "快问题", "快答"]


@pytest.mark.asyncio
async def test_tool_calls_are_attributed_to_the_turn_that_made_them(tmp_path):
    """工具流水写在本轮 messages 之前，没有入口处的号就会挂到上一轮去。"""
    import sqlite3
    from contextlib import closing

    from csagent.audit.trace_buffer import get_audit_buffer

    db = tmp_path / "csagent.db"

    async def _run_with_tool(*_args, **_kwargs):
        buffer = get_audit_buffer()
        buffer.tool_calls.append({
            "tool_name": "run_script",
            "arguments_preview": "a.py",
            "result_preview": "命中 3 条",
            "status": "success",
        })
        return DomainTaskResult(
            task_id="t", domain="generic", status="success",
            answer_draft="查好了", confidence=0.9, structured_data={},
        )

    agent = _sqlite_agent(db, _run_with_tool)
    for text in ("第一问", "第二问"):
        async for _ in agent.handle_message_stream(session_id="s-tool", text=text):
            pass

    with closing(sqlite3.connect(db)) as conn:
        turn_ids = [
            row[0] for row in conn.execute(
                "SELECT turn_id FROM tool_calls WHERE session_id = 's-tool' ORDER BY id"
            )
        ]

    assert turn_ids == [1, 2], "第二轮的工具调用挂到 turn 1 上，说明号没透传进来"

