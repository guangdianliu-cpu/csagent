"""未完成待办注入上下文（M3.5）。"""

from __future__ import annotations

from csagent.memory.context_pipeline import ContextPipeline
from csagent.memory.short_term import ShortTermMemory
from csagent.tools.packs.todo_pack import (
    TodoStore,
    _global_todo_store,
    format_open_todos_for_context,
)


def setup_function() -> None:
    _global_todo_store.clear()


def teardown_function() -> None:
    _global_todo_store.clear()


def test_format_open_todos_skips_done_items():
    _global_todo_store["web:u1"] = {
        "t1": {"id": "t1", "content": "还在做", "status": "in_progress"},
        "t2": {"id": "t2", "content": "做完了", "status": "completed"},
        "t3": {"id": "t3", "content": "排队", "status": "pending"},
    }
    block = format_open_todos_for_context("web:u1")
    assert "还在做" in block
    assert "排队" in block
    assert "做完了" not in block


def test_format_open_todos_recovers_from_disk(tmp_path, monkeypatch):
    store = TodoStore(tmp_path)
    store.save(
        "web:u2",
        {"t1": {"id": "t1", "content": "磁盘上的待办", "status": "pending"}},
    )
    monkeypatch.setattr("csagent.tools.packs.todo_pack._persist_dir", str(tmp_path))
    _global_todo_store.clear()
    block = format_open_todos_for_context("web:u2")
    assert "磁盘上的待办" in block


def test_pipeline_injects_open_todos():
    memory = ShortTermMemory()
    pipeline = ContextPipeline(short_term_memory=memory, history_window=4)
    _global_todo_store["s"] = {
        "t1": {"id": "t1", "content": "写完回归脚本", "status": "pending"},
    }
    frame = pipeline.build(
        session_id="s",
        user_text="我做到哪了",
        system_prompt="你是助手",
        tool_schemas=[],
    )
    assert "当前未完成待办" in frame.context_summary
    assert "写完回归脚本" in frame.context_summary
