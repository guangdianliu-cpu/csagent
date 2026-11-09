"""TODO 持久化（M3.2）：TodoStore 落盘 + todo_read 跨「进程重启」恢复。"""

from __future__ import annotations

import pytest

from csagent.react.models import CustomerScope
from csagent.tools.packs.todo_pack import (
    TodoReadTool,
    TodoStore,
    TodoWriteTool,
    _global_todo_store,
)


def _scope(session_id: str = "test-todo-persist") -> CustomerScope:
    return CustomerScope(session_id=session_id, channel="test")


@pytest.fixture(autouse=True)
def _clear_store():
    _global_todo_store.clear()
    yield
    _global_todo_store.clear()


@pytest.mark.asyncio
async def test_todo_write_persists_to_disk(tmp_path):
    store = TodoStore(tmp_path)
    tool = TodoWriteTool(store)
    scope = _scope("s1")

    await tool.execute(
        {"todos": [{"id": "t1", "content": "写文档", "status": "pending"}]}, scope
    )

    on_disk = store.load("s1")
    assert on_disk.get("t1", {}).get("content") == "写文档"


@pytest.mark.asyncio
async def test_todo_read_recovers_after_process_restart(tmp_path):
    """模拟进程重启：清空内存缓存后，todo_read 仍能从磁盘恢复。"""
    store = TodoStore(tmp_path)
    write_tool = TodoWriteTool(store)
    scope = _scope("s2")

    await write_tool.execute(
        {
            "todos": [
                {"id": "t1", "content": "任务一", "status": "completed"},
                {"id": "t2", "content": "任务二", "status": "in_progress"},
            ]
        },
        scope,
    )

    # 模拟进程重启：清空内存态，磁盘文件仍存在
    _global_todo_store.clear()

    read_tool = TodoReadTool(store)
    result = await read_tool.execute({}, scope)

    assert "任务一" in result
    assert "任务二" in result
    assert "completed" in result
    assert "in_progress" in result


@pytest.mark.asyncio
async def test_todo_read_empty_when_no_history(tmp_path):
    store = TodoStore(tmp_path)
    read_tool = TodoReadTool(store)
    result = await read_tool.execute({}, _scope("brand-new-session"))
    assert "没有待办" in result


@pytest.mark.asyncio
async def test_todo_write_without_store_still_works_in_memory():
    """未注入 TodoStore（如旧测试直接 new TodoWriteTool()）时仍保持内存态行为。"""
    tool = TodoWriteTool()
    result = await tool.execute(
        {"todos": [{"id": "t1", "content": "任务", "status": "pending"}]},
        _scope("no-store-session"),
    )
    assert "1" in result


def test_todo_store_load_missing_session_returns_empty(tmp_path):
    store = TodoStore(tmp_path)
    assert store.load("does-not-exist") == {}


def test_todo_store_sanitizes_session_id_for_path_safety(tmp_path):
    store = TodoStore(tmp_path)
    store.save("../../etc/passwd", {"t1": {"id": "t1", "content": "x", "status": "pending"}})
    # 清洗后应落在 base_dir 下，不应逃逸到外部路径
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].parent == tmp_path
