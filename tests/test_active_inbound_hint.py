"""MessageGateway：当前用户文件指针注入策略。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from csagent.channels.message_gateway import (
    MessageGateway,
    _user_refers_to_prior_file,
)
from csagent.channels.workspace_uploads import set_active_inbound


@pytest.fixture
def upload_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PROJECT_ROOT + data/uploads 布局，供 get_active_inbound 校验文件存在。"""
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True)
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", uploads
    )
    return tmp_path, uploads


def _seed_active(uploads: Path, project: Path, sid: str, name: str) -> str:
    inbound = uploads / sid / "inbound"
    inbound.mkdir(parents=True)
    (inbound / name).write_bytes(b"xlsx")
    rel = f"data/uploads/{sid}/inbound/{name}"
    assert (project / rel).is_file()
    set_active_inbound(sid, rel, root=uploads)
    return rel


def test_user_refers_to_prior_file_markers():
    assert _user_refers_to_prior_file("帮我看看这个文件里的持仓")
    assert _user_refers_to_prior_file("按上面的表格再筛一遍")
    assert not _user_refers_to_prior_file("研究宏观周期下的策略选择情况")
    assert not _user_refers_to_prior_file("优选二级债基 业绩前50%")


def test_plain_question_does_not_inject_stale_inbound(upload_layout):
    project, uploads = upload_layout
    sid = "s-plain"
    _seed_active(uploads, project, sid, "旧文件.xlsx")

    gw = MessageGateway(main_agent=MagicMock())
    out = gw._append_active_inbound_hint(
        "研究宏观周期下的策略选择情况",
        sid,
        this_turn_has_file=False,
    )
    assert "当前用户文件" not in out
    assert "旧文件.xlsx" not in out
    assert out == "研究宏观周期下的策略选择情况"


def test_deixis_injects_active_inbound(upload_layout):
    project, uploads = upload_layout
    sid = "s-deixis"
    rel = _seed_active(uploads, project, sid, "持仓.xlsx")

    gw = MessageGateway(main_agent=MagicMock())
    out = gw._append_active_inbound_hint(
        "把这个文件按规模再排一下",
        sid,
        this_turn_has_file=False,
    )
    assert "当前用户文件" in out
    assert rel in out
    assert "不要因此" in out


def test_this_turn_file_skips_duplicate_pointer(upload_layout):
    project, uploads = upload_layout
    sid = "s-new"
    _seed_active(uploads, project, sid, "新.xlsx")

    gw = MessageGateway(main_agent=MagicMock())
    body = "[附件 file: 新.xlsx]\n工作区路径: data/uploads/s-new/inbound/新.xlsx"
    out = gw._append_active_inbound_hint(body, sid, this_turn_has_file=True)
    assert out == body
    assert "当前用户文件" not in out
