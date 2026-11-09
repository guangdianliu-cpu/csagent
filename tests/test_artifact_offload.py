"""大工具结果外置（M3.1）测试。

主张三条：
1. 超限结果全文落盘，上下文只留占位——被「截断」的部分还能按页取回。
2. 取回必须校验会话归属；登记路径若被改到外置目录之外，拒绝读取。
3. 登记库不可用时回落到原来的硬截断，不能让已经跑完的工具调用失败。
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from csagent.memory.artifact_offload import offload_tool_result, read_artifact_slice
from csagent.memory.artifact_store import (
    ArtifactRecord,
    ArtifactStore,
    set_artifact_store,
)
from csagent.react.handlers import _shrink_tool_result
from csagent.react.models import CustomerScope
from csagent.tools.packs.artifact_pack import ReadArtifactTool


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """把外置根目录和项目根都指到临时目录，避免测试往仓库 data/ 里写。"""
    monkeypatch.setattr("csagent.memory.artifact_offload.PROJECT_ROOT", tmp_path)
    store = ArtifactStore(tmp_path / "csagent.db")
    set_artifact_store(store)
    yield store
    set_artifact_store(None)


def _array(n: int = 40) -> str:
    return json.dumps(
        [{"code": f"{i:04d}", "name": f"item-{i}", "value": i * 10} for i in range(n)],
        ensure_ascii=False,
    )


def _artifact_id(placeholder: str) -> str:
    match = re.search(r"artifact_id: ([0-9a-f]{32})", placeholder)
    assert match, placeholder
    return match.group(1)


# ---------------------------------------------------------------- 外置


def test_short_result_is_not_offloaded(ledger):
    assert offload_tool_result("short", session_id="s1", tool_name="t", limit=100) is None


def test_json_array_is_saved_as_jsonl_and_placeholder_keeps_id(ledger, tmp_path):
    raw = _array(50)
    assert len(raw) > 200

    placeholder = offload_tool_result(
        raw, session_id="web:u1", tool_name="run_script", limit=200
    )

    assert placeholder is not None
    assert "[大结果已外置]" in placeholder
    assert "一个字都没有丢" in placeholder
    assert "不要据此作答" in placeholder
    artifact_id = _artifact_id(placeholder)

    record = ledger.get(artifact_id)
    assert record is not None
    assert record.session_id == "web:u1"
    assert record.kind == "tool_result"
    assert record.filename.endswith(".jsonl")

    disk = tmp_path / record.path
    lines = disk.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    assert json.loads(lines[0])["code"] == "0000"


def test_placeholder_lists_columns_from_records(ledger):
    placeholder = offload_tool_result(
        _array(30), session_id="s1", tool_name="t", limit=50
    )
    assert "字段: code, name, value" in placeholder


def test_plain_text_keeps_every_line(ledger, tmp_path):
    raw = "\n".join(f"line-{i}" for i in range(80))
    placeholder = offload_tool_result(raw, session_id="s1", tool_name="t", limit=50)
    artifact_id = _artifact_id(placeholder)
    text = (tmp_path / ledger.get(artifact_id).path).read_text(encoding="utf-8")
    assert text.splitlines()[-1] == "line-79"


def test_offload_skipped_when_ledger_is_down(tmp_path, monkeypatch):
    """登记失败必须返回 None，让调用方回落硬截断——给出一个取不回的 id 更糟。"""
    monkeypatch.setattr("csagent.memory.artifact_offload.PROJECT_ROOT", tmp_path)
    set_artifact_store(None)
    try:
        assert (
            offload_tool_result(_array(30), session_id="s1", tool_name="t", limit=50)
            is None
        )
    finally:
        set_artifact_store(None)


# ---------------------------------------------------------------- 读回


def test_read_back_paginates_and_projects_columns(ledger, tmp_path):
    placeholder = offload_tool_result(
        _array(30), session_id="s1", tool_name="t", limit=50
    )
    record = ledger.get(_artifact_id(placeholder))

    page = read_artifact_slice(
        record, offset=1, limit=5, columns=["code"], artifacts_root=tmp_path / "data" / "artifacts"
    )

    assert "第 1~5 行，共 30 行" in page
    assert '"code": "0000"' in page
    assert "name" not in page
    assert "下一页" in page


def test_last_page_says_end(ledger, tmp_path):
    placeholder = offload_tool_result(
        _array(5), session_id="s1", tool_name="t", limit=20
    )
    record = ledger.get(_artifact_id(placeholder))
    page = read_artifact_slice(
        record, offset=1, limit=20, artifacts_root=tmp_path / "data" / "artifacts"
    )
    assert "[已到末尾]" in page


def test_refuses_path_outside_artifacts_root(ledger, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password", encoding="utf-8")
    record = ArtifactRecord(
        id="deadbeef" * 4,
        session_id="s1",
        kind="tool_result",
        path=str(secret),
        filename="secret.txt",
        size=8,
        created_at=0.0,
    )

    out = read_artifact_slice(
        record, artifacts_root=tmp_path / "data" / "artifacts"
    )
    assert "越出允许目录" in out
    assert "password" not in out


# ---------------------------------------------------------------- 工具 + 回灌


@pytest.mark.asyncio
async def test_read_artifact_tool_rejects_other_session(ledger):
    placeholder = offload_tool_result(
        _array(10), session_id="web:u1", tool_name="t", limit=20
    )
    artifact_id = _artifact_id(placeholder)
    tool = ReadArtifactTool()

    denied = await tool.execute(
        {"artifact_id": artifact_id}, CustomerScope(session_id="web:u2")
    )
    allowed = await tool.execute(
        {"artifact_id": artifact_id, "offset": 1, "limit": 3},
        CustomerScope(session_id="web:u1"),
    )

    assert denied == "错误: artifact 不存在或不属于当前会话。"
    assert "0000" in allowed


def test_shrink_offloads_when_over_limit(ledger):
    ctx = MagicMock()
    ctx.request.customer_scope.session_id = "s1"
    raw = _array(40)

    out = _shrink_tool_result(
        raw, ctx=ctx, tool_name="run_script", limit=80, truncate_fn=lambda s: "CUT"
    )

    assert out != "CUT"
    assert "[大结果已外置]" in out
    assert "CUT" not in out


def test_shrink_falls_back_to_truncate_without_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr("csagent.memory.artifact_offload.PROJECT_ROOT", tmp_path)
    set_artifact_store(None)
    ctx = MagicMock()
    ctx.request.customer_scope.session_id = "s1"

    out = _shrink_tool_result(
        "x" * 200, ctx=ctx, tool_name="t", limit=50, truncate_fn=lambda s: "FALLBACK"
    )

    assert out == "FALLBACK"
