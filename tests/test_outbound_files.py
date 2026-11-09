"""出站文件队列、分片上传与 send_file 工具。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.channels.outbound_files import (
    OutboundFile,
    clear_outbound_files,
    detect_media_type,
    drain_outbound_files,
    enqueue_outbound_file,
    peek_outbound_files,
)
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.react.models import CustomerScope
from csagent.tools.packs.workspace_files_pack import SendFileTool
from csagent.wecom.bridge import WeComAgentBridge
from csagent.wecom.media_upload import media_message_body, upload_media


@pytest.fixture(autouse=True)
def _clear_queues():
    clear_outbound_files()
    yield
    clear_outbound_files()


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s-out", channel="wecom")


def test_detect_media_type():
    assert detect_media_type("a.png") == "image"
    assert detect_media_type("a.xlsx") == "file"
    assert media_message_body("file", "MID") == {
        "msgtype": "file",
        "file": {"media_id": "MID"},
    }


@pytest.mark.asyncio
async def test_send_file_enqueues(tmp_path: Path, scope: CustomerScope):
    path = tmp_path / "out.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    tool = SendFileTool(root=tmp_path, session_scoped=False)
    result = await tool.execute({"path": "out.csv"}, scope)
    assert "出站队列" in result
    pending = peek_outbound_files(scope.session_id)
    assert len(pending) == 1
    assert pending[0].filename == "out.csv"
    assert pending[0].media_type == "file"


@pytest.mark.asyncio
async def test_send_file_registers_an_artifact(tmp_path: Path, scope: CustomerScope):
    """入队是本轮内的交接，登记是永久清单——send_file 两件事都要做（M1c）。"""
    from csagent.memory.artifact_store import ArtifactStore, set_artifact_store

    path = tmp_path / "out.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "csagent.db")
    set_artifact_store(store)
    try:
        await SendFileTool(root=tmp_path, session_scoped=False).execute(
            {"path": "out.csv"}, scope
        )
        found = store.find(session_id=scope.session_id, kind="outbound")
    finally:
        set_artifact_store(None)

    assert [r.filename for r in found] == ["out.csv"]


@pytest.mark.asyncio
async def test_send_file_still_works_without_an_artifact_ledger(
    tmp_path: Path, scope: CustomerScope
):
    """未接清单时（非 sqlite 后端 / 测试）发送照常，登记静默跳过。"""
    from csagent.memory.artifact_store import set_artifact_store

    path = tmp_path / "out.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    set_artifact_store(None)

    result = await SendFileTool(root=tmp_path, session_scoped=False).execute(
        {"path": "out.csv"}, scope
    )

    assert "出站队列" in result
    assert len(peek_outbound_files(scope.session_id)) == 1


@pytest.mark.asyncio
async def test_upload_media_chunk_protocol():
    acks = [
        {"errcode": 0, "body": {"upload_id": "UP1"}},
        {"errcode": 0, "body": {}},
        {"errcode": 0, "body": {"media_id": "MEDIA123", "type": "file"}},
    ]

    async def _send_reply(req_id, body, cmd):
        return acks.pop(0)

    client = MagicMock()
    client._ws_manager = MagicMock()
    client._ws_manager.send_reply = AsyncMock(side_effect=_send_reply)

    media_id = await upload_media(
        client,
        b"hello-world-file",
        filename="demo.txt",
        media_type="file",
    )
    assert media_id == "MEDIA123"
    assert client._ws_manager.send_reply.await_count == 3
    cmds = [c.args[2] for c in client._ws_manager.send_reply.await_args_list]
    assert cmds == [
        "aibot_upload_media_init",
        "aibot_upload_media_chunk",
        "aibot_upload_media_finish",
    ]


@pytest.mark.asyncio
async def test_bridge_flushes_outbound_after_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    captured = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="处理完成")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream

    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.STUB),
    )
    settings = MagicMock()
    settings.wecom_stream_interval_ms = 10
    settings.wecom_welcome = ""

    bridge = WeComAgentBridge(
        main_agent=main_agent,
        settings=settings,
        message_gateway=gateway,
    )
    out = tmp_path / "result.txt"
    out.write_text("done\n", encoding="utf-8")
    enqueue_outbound_file(
        "wecom:single:u9",
        OutboundFile(
            path=str(out),
            workspace_path="result.txt",
            filename="result.txt",
            media_type="file",
        ),
    )

    client = MagicMock()
    client.reply_stream = AsyncMock()
    client.send_message = AsyncMock()
    client._ws_manager = MagicMock()

    async def _send_reply(req_id, body, cmd):
        if cmd.endswith("init"):
            return {"errcode": 0, "body": {"upload_id": "U"}}
        if cmd.endswith("finish"):
            return {"errcode": 0, "body": {"media_id": "M1"}}
        return {"errcode": 0, "body": {}}

    client._ws_manager.send_reply = AsyncMock(side_effect=_send_reply)
    bridge._client = client

    await bridge._stream_via_gateway(
        {"headers": {"req_id": "r1"}, "body": {}},
        UnifiedMessage(
            channel="wecom",
            session_id="wecom:single:u9",
            text="请发文件",
        ),
        userid="u9",
        is_group=False,
    )

    client.send_message.assert_awaited()
    sent_body = client.send_message.await_args.args[1]
    assert sent_body["msgtype"] == "file"
    assert sent_body["file"]["media_id"] == "M1"
    assert drain_outbound_files("wecom:single:u9") == []


@pytest.mark.asyncio
async def test_bridge_embeds_https_link_instead_of_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "csagent.api.router_files._public_base_url",
        lambda: "https://demo.vicp.fun",
    )

    async def _stream(**kwargs):
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="处理完成")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.STUB),
    )
    settings = MagicMock()
    settings.wecom_stream_interval_ms = 10
    settings.wecom_welcome = ""
    bridge = WeComAgentBridge(
        main_agent=main_agent,
        settings=settings,
        message_gateway=gateway,
    )
    out = tmp_path / "result.txt"
    out.write_text("done\n", encoding="utf-8")
    enqueue_outbound_file(
        "wecom:single:u9",
        OutboundFile(
            path=str(out),
            workspace_path="result.txt",
            filename="result.txt",
            media_type="file",
        ),
    )
    client = MagicMock()
    client.reply_stream = AsyncMock()
    client.send_message = AsyncMock()
    bridge._client = client

    await bridge._stream_via_gateway(
        {"headers": {"req_id": "r1"}, "body": {}},
        UnifiedMessage(
            channel="wecom",
            session_id="wecom:single:u9",
            text="请发文件",
        ),
        userid="u9",
        is_group=False,
    )

    client.send_message.assert_not_called()
    finish = client.reply_stream.await_args_list[-1]
    assert finish.kwargs.get("finish") is True
    content = finish.args[2]
    assert "处理完成" in content
    assert "result.txt" in content
    assert "[result.txt](" in content
    assert "https://demo.vicp.fun/api/files/download" in content
    assert drain_outbound_files("wecom:single:u9") == []


@pytest.mark.asyncio
async def test_gateway_hybrid_preview_truncates(tmp_path: Path, monkeypatch):
    big = "字" * 5000
    src = tmp_path / "big.txt"
    src.write_text(big, encoding="utf-8")
    uploads = tmp_path / "uploads"

    monkeypatch.setattr(
        "csagent.channels.message_gateway.stage_attachment",
        lambda att, session_id, index=0, update_pointer=True: _stage(
            att,
            session_id=session_id,
            index=index,
            uploads=uploads,
            root=tmp_path,
            update_pointer=update_pointer,
        ),
    )

    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.STUB),
        preview_chars=200,
    )
    msg = UnifiedMessage(
        channel="http_api",
        session_id="s-big",
        text="看文件",
        attachments=[
            Attachment(
                type=AttachmentType.FILE,
                filename="big.txt",
                local_path=str(src),
            )
        ],
    )
    _ = [e async for e in gateway.handle(msg)]
    text = captured.get("text") or ""
    assert "工作区路径" in text
    assert "已截断" in text
    assert "read_file" in text
    # 不应把全文灌进提示词
    assert text.count("字") < 1000


def _stage(att, *, session_id, index, uploads, root, update_pointer=True):
    from csagent.channels.workspace_uploads import stage_attachment

    return stage_attachment(
        att,
        session_id=session_id,
        index=index,
        uploads_root=uploads,
        project_root=root,
        update_pointer=update_pointer,
    )
