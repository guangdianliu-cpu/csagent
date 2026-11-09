"""test_todo_pack.py — todo_write 工具测试。"""

from __future__ import annotations

import pytest

from csagent.react.models import CustomerScope
from csagent.session_state import SessionState, session_state_var
from csagent.tools.packs.todo_pack import TodoWriteTool, _global_todo_store


def _scope(session_id: str = "test-todo") -> CustomerScope:
    return CustomerScope(session_id=session_id, channel="test")


@pytest.fixture(autouse=True)
def _clear_store():
    _global_todo_store.clear()
    yield
    _global_todo_store.clear()


@pytest.mark.asyncio
async def test_create_todos():
    tool = TodoWriteTool()
    result = await tool.execute({
        "todos": [
            {"id": "t1", "content": "写代码", "status": "pending"},
            {"id": "t2", "content": "写测试", "status": "in_progress"},
        ]
    }, _scope())

    assert "2" in result
    assert "pending" in result
    assert "in_progress" in result


@pytest.mark.asyncio
async def test_merge_todos():
    tool = TodoWriteTool()
    scope = _scope()

    # 第一次创建
    await tool.execute({
        "todos": [{"id": "t1", "content": "任务一", "status": "pending"}]
    }, scope)

    # 合并新增
    result = await tool.execute({
        "todos": [{"id": "t2", "content": "任务二", "status": "pending"}],
        "merge": True,
    }, scope)

    assert "2" in result


@pytest.mark.asyncio
async def test_replace_todos():
    tool = TodoWriteTool()
    scope = _scope()

    # 第一次创建
    await tool.execute({
        "todos": [{"id": "t1", "content": "任务一", "status": "pending"}]
    }, scope)

    # 替换（merge=False）
    result = await tool.execute({
        "todos": [{"id": "t2", "content": "任务二", "status": "completed"}],
        "merge": False,
    }, scope)

    assert "1" in result  # 只有 1 个
    assert "completed" in result


@pytest.mark.asyncio
async def test_status_update():
    tool = TodoWriteTool()
    scope = _scope()

    await tool.execute({
        "todos": [{"id": "t1", "content": "任务", "status": "pending"}]
    }, scope)

    result = await tool.execute({
        "todos": [{"id": "t1", "content": "任务", "status": "completed"}],
        "merge": True,
    }, scope)

    assert "completed" in result


@pytest.mark.asyncio
async def test_invalid_status():
    tool = TodoWriteTool()
    result = await tool.execute({
        "todos": [{"id": "t1", "content": "任务", "status": "invalid_status"}]
    }, _scope())

    assert "警告" in result or "无效" in result


@pytest.mark.asyncio
async def test_missing_fields():
    tool = TodoWriteTool()

    # 缺少 id
    result = await tool.execute({
        "todos": [{"content": "任务", "status": "pending"}]
    }, _scope())
    assert "警告" in result or "id" in result

    # 缺少 content
    result = await tool.execute({
        "todos": [{"id": "t1", "status": "pending"}]
    }, _scope())
    assert "警告" in result or "content" in result


@pytest.mark.asyncio
async def test_progress_pushes_checklist_with_checkmarks():
    """推到渠道的是整份勾选清单，完成一项打勾，不推状态日志。"""
    seen: list[str] = []

    async def on_progress(text: str) -> None:
        seen.append(text)

    token = session_state_var.set(SessionState(on_progress=on_progress))
    try:
        tool = TodoWriteTool()
        scope = _scope("checklist")
        await tool.execute(
            {
                "todos": [
                    {"id": "t1", "content": "查净值", "status": "pending"},
                    {"id": "t2", "content": "写报告", "status": "in_progress"},
                ]
            },
            scope,
        )
        await tool.execute(
            {
                "todos": [{"id": "t1", "content": "查净值", "status": "completed"}],
                "merge": True,
            },
            scope,
        )
    finally:
        session_state_var.reset(token)

    assert seen[0] == "待办\n☐ 查净值\n⏳ 写报告"
    assert seen[1] == "待办\n✅ 查净值\n⏳ 写报告"
    assert "TODO" not in seen[1]
    assert "pending" not in seen[1]


def test_checklist_collapses_when_all_done():
    from csagent.user_progress import format_todo_checklist

    text = format_todo_checklist([
        {"content": "查净值", "status": "completed"},
        {"content": "写报告", "status": "cancelled"},
    ])
    assert text == "待办"


@pytest.mark.asyncio
async def test_empty_todos():
    tool = TodoWriteTool()
    result = await tool.execute({"todos": []}, _scope())
    assert "错误" in result or "0" in result
