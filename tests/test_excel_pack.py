"""ExcelToolPack 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.react.models import CustomerScope
from csagent.tools.packs.excel_pack import ExcelTool


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s-xlsx", channel="http_api")


@pytest.mark.asyncio
async def test_excel_create_read_filter_write(tmp_path: Path, scope: CustomerScope):
    tool = ExcelTool(root=tmp_path, max_rows=50)
    path = "demo.xlsx"
    # Agent 产出落 outbound/；裸名读取会按 inbound → outbound 顺序找到它
    expected = tmp_path / "data" / "uploads" / "s-xlsx" / "outbound" / "demo.xlsx"

    created = await tool.execute(
        {
            "action": "create",
            "path": path,
            "sheet": "产品",
            "headers": ["代码", "名称", "状态"],
            "rows": [
                ["110022", "易方达消费", "可申购"],
                ["000001", "华夏成长", "暂停"],
                ["110022", "重复行", "可申购"],
            ],
        },
        scope,
    )
    assert "已创建" in created
    assert expected.is_file()

    sheets = await tool.execute({"action": "list_sheets", "path": path}, scope)
    assert "产品" in sheets

    read = await tool.execute(
        {"action": "read", "path": path, "sheet": "产品", "max_rows": 10},
        scope,
    )
    assert "110022" in read
    assert "易方达消费" in read

    filtered = await tool.execute(
        {
            "action": "filter",
            "path": path,
            "sheet": "产品",
            "column": "代码",
            "op": "eq",
            "value": "110022",
        },
        scope,
    )
    assert "匹配 2 行" in filtered or "易方达消费" in filtered

    written = await tool.execute(
        {
            "action": "write",
            "path": path,
            "sheet": "产品",
            "cells": [{"row": 2, "col": "C", "value": "已更新"}],
        },
        scope,
    )
    assert "已写入" in written
    assert "send_file" in written

    read2 = await tool.execute(
        {"action": "read", "path": path, "sheet": "产品", "start_row": 2, "max_rows": 1},
        scope,
    )
    assert "已更新" in read2


@pytest.mark.asyncio
async def test_excel_create_rejects_empty(tmp_path: Path, scope: CustomerScope):
    tool = ExcelTool(root=tmp_path)
    result = await tool.execute(
        {"action": "create", "path": "empty.xlsx", "headers": [], "rows": []},
        scope,
    )
    assert "错误" in result
    assert "空表" in result or "headers" in result
