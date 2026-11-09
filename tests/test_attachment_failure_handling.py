"""附件取不到 / 重复入站时的行为。

企微群聊里文件只能靠引用抵达，引用段拿不到下载凭证时若静默丢弃，
Agent 会拿会话目录里的其它文件顶替用户刚发的这份，且从回复里看不出错在哪。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.channels.workspace_uploads import get_active_inbound, stage_attachment
from csagent.stream_events import AgentEventType, AgentStreamEvent


def _gateway(captured: dict) -> MessageGateway:
    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream
    return MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.STUB),
    )


def test_stage_attachment_reuses_file_already_in_session_dir(
    tmp_path: Path, monkeypatch
):
    """源已在 inbound 内时原地复用，不再每轮复制出 x_1 / x_2 副本。"""
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    uploads = tmp_path / "uploads"
    sess_dir = uploads / "s-dup" / "inbound"
    sess_dir.mkdir(parents=True)
    src = sess_dir / "同一个.xlsx"
    src.write_bytes(b"same bytes")

    for _ in range(3):
        att = Attachment(
            type=AttachmentType.FILE,
            filename="同一个.xlsx",
            local_path=str(src),
        )
        rel = stage_attachment(
            att,
            session_id="s-dup",
            uploads_root=uploads,
            project_root=tmp_path,
        )
        assert rel is not None
        assert rel.endswith("同一个.xlsx")

    assert sorted(p.name for p in sess_dir.iterdir()) == ["同一个.xlsx"]


@pytest.mark.asyncio
async def test_quote_attachment_does_not_steal_pointer_from_main(
    tmp_path: Path, monkeypatch
):
    """主消息带新文件 + 引用带旧文件时，「当前用户文件」必须是新文件。"""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", uploads
    )
    new_src = tmp_path / "新文件.xlsx"
    new_src.write_bytes(b"new")
    old_src = tmp_path / "旧文件.xlsx"
    old_src.write_bytes(b"old")

    old_att = Attachment(
        type=AttachmentType.FILE, filename="旧文件.xlsx", local_path=str(old_src)
    )
    old_att.metadata["from_quote"] = True

    captured: dict = {}
    msg = UnifiedMessage(
        channel="wecom",
        session_id="s-mix",
        text="看这个文件",
        # iter_file_sections 的真实顺序：主消息在前、引用在后
        attachments=[
            Attachment(
                type=AttachmentType.FILE,
                filename="新文件.xlsx",
                local_path=str(new_src),
            ),
            old_att,
        ],
    )
    _ = [e async for e in _gateway(captured).handle(msg)]

    active = get_active_inbound("s-mix", root=uploads, project_root=tmp_path)
    assert active is not None
    assert active.endswith("inbound/新文件.xlsx")
    # 引用件仍应完成暂存，只是不抢指针
    assert (uploads / "s-mix" / "inbound" / "旧文件.xlsx").is_file()


@pytest.mark.asyncio
async def test_quote_only_attachment_still_sets_pointer(tmp_path: Path, monkeypatch):
    """群聊场景：文件只能经引用抵达，此时引用必须更新指针。"""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", uploads
    )
    src = tmp_path / "群里的.xlsx"
    src.write_bytes(b"grp")
    quoted = Attachment(
        type=AttachmentType.FILE, filename="群里的.xlsx", local_path=str(src)
    )
    quoted.metadata["from_quote"] = True

    captured: dict = {}
    msg = UnifiedMessage(
        channel="wecom",
        session_id="s-grp",
        text="看这个文件",
        attachments=[quoted],
    )
    _ = [e async for e in _gateway(captured).handle(msg)]

    active = get_active_inbound("s-grp", root=uploads, project_root=tmp_path)
    assert active is not None
    assert active.endswith("inbound/群里的.xlsx")


@pytest.mark.asyncio
async def test_lost_attachment_is_reported_with_reason(tmp_path: Path, monkeypatch):
    """附件全军覆没时，要说清原因并禁止拿别的文件顶替。"""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", uploads
    )
    # 会话目录里已有一份旧文件，Agent 不该拿它顶替
    sess_dir = uploads / "s-lost"
    sess_dir.mkdir(parents=True)
    (sess_dir / "上一份.xlsx").write_bytes(b"old")

    captured: dict = {}
    lost = Attachment(
        type=AttachmentType.FILE,
        filename="用户刚发的.xlsx",
        metadata={"download_error": "消息里没有可下载的 url"},
    )
    msg = UnifiedMessage(
        channel="wecom",
        session_id="s-lost",
        text="看下这个文件",
        attachments=[lost],
    )
    _ = [e async for e in _gateway(captured).handle(msg)]
    text = captured.get("text") or ""

    assert "未取到文件内容" in text
    assert "消息里没有可下载的 url" in text
    assert "重新发送" in text
    assert "上一份.xlsx" not in text


@pytest.mark.asyncio
async def test_partial_failure_does_not_trigger_lost_notice(
    tmp_path: Path, monkeypatch
):
    """一份成功一份失败时，失败仍要如实说明，但不走全军覆没的告警分支。"""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr("csagent.channels.workspace_uploads.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", uploads
    )
    src = tmp_path / "成功的.xlsx"
    src.write_bytes(b"ok")

    captured: dict = {}
    msg = UnifiedMessage(
        channel="wecom",
        session_id="s-partial",
        text="看下这两个",
        attachments=[
            Attachment(
                type=AttachmentType.FILE,
                filename="成功的.xlsx",
                local_path=str(src),
            ),
            Attachment(
                type=AttachmentType.FILE,
                filename="失败的.xlsx",
                metadata={"download_error": "下载超时"},
            ),
        ],
    )
    _ = [e async for e in _gateway(captured).handle(msg)]
    text = captured.get("text") or ""

    assert "成功的.xlsx" in text
    assert "下载超时" in text        # 失败那份仍要如实说明
    assert "重新发送" not in text    # 但不该走全军覆没的告警分支
