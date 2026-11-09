"""企微媒体下载与入站组装单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import AttachmentType
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.wecom.bridge import WeComAgentBridge, _compose_user_text
from csagent.wecom.media import (
    download_files_as_attachments,
    download_images_as_attachments,
    extract_plaintext_from_body,
    iter_file_sections,
    iter_image_sections,
    save_media_bytes,
    upload_dir_for_session,
)


def test_iter_image_sections_from_image_and_quote():
    body = {
        "msgtype": "image",
        "image": {"url": "https://example.com/a", "aeskey": "k1"},
        "quote": {
            "msgtype": "image",
            "image": {"url": "https://example.com/b", "aeskey": "k2"},
        },
    }
    sections = iter_image_sections(body)
    assert len(sections) == 2
    assert sections[0]["url"].endswith("/a")
    assert sections[1]["aeskey"] == "k2"


def test_iter_image_sections_from_mixed():
    body = {
        "msgtype": "mixed",
        "mixed": {
            "msg_item": [
                {"msgtype": "text", "text": {"content": "@助手 看图"}},
                {
                    "msgtype": "image",
                    "image": {"url": "https://example.com/m", "aeskey": "km"},
                },
            ]
        },
    }
    sections = iter_image_sections(body)
    assert len(sections) == 1
    assert sections[0]["url"].endswith("/m")


def test_extract_plaintext_strips_at():
    body = {
        "msgtype": "text",
        "text": {"content": "@产品助手 110022净值"},
    }
    text = extract_plaintext_from_body(
        body, strip_at=lambda s: s.replace("@产品助手", "").strip()
    )
    assert text == "110022净值"


def test_upload_dir_and_save(tmp_path: Path):
    dest = upload_dir_for_session("wecom:single:u1", root=tmp_path)
    path = save_media_bytes(b"\x89PNG", dest_dir=dest, filename="a.png")
    assert path.exists()
    assert path.read_bytes().startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_download_images_as_attachments(tmp_path: Path):
    client = MagicMock()
    client.download_file = AsyncMock(return_value=(b"imagedata", "shot.jpg"))
    attachments = await download_images_as_attachments(
        client,
        [{"url": "https://x/y", "aeskey": "abc"}],
        dest_dir=tmp_path,
    )
    assert len(attachments) == 1
    att = attachments[0]
    assert att.type == AttachmentType.IMAGE
    assert att.local_path
    assert Path(att.local_path).read_bytes() == b"imagedata"
    client.download_file.assert_awaited_once_with("https://x/y", "abc")


@pytest.mark.asyncio
async def test_bridge_image_message_goes_through_gateway(tmp_path: Path, monkeypatch):
    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="已看图")

    main_agent = MagicMock()

    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.MULTIMODAL),
    )
    # Gateway 内部会调 handle_message_stream
    main_agent.handle_message_stream = _stream

    settings = MagicMock()
    settings.wecom_stream_interval_ms = 50
    settings.wecom_welcome = ""

    bridge = WeComAgentBridge(
        main_agent=main_agent,
        settings=settings,
        message_gateway=gateway,
    )
    client = MagicMock()
    client.download_file = AsyncMock(return_value=(b"\x89PNG\r\n", "x.png"))
    client.reply_stream = AsyncMock()
    bridge._client = client

    monkeypatch.setattr(
        "csagent.wecom.bridge.upload_dir_for_session",
        lambda session_id, root=None: tmp_path,
    )

    frame = {
        "body": {
            "msgtype": "image",
            "image": {"url": "https://corp/img", "aeskey": "key"},
            "from": {"userid": "u1"},
            "chattype": "single",
        }
    }
    await bridge._handle_inbound_message(frame)

    assert captured.get("vision_parts")
    assert captured["vision_parts"][0]["type"] == "image_url"
    assert "图片" in captured.get("text", "") or "看" in captured.get("text", "")
    client.reply_stream.assert_awaited()


def test_compose_user_text_unchanged():
    assert _compose_user_text(user_text="问净值", quote=None) == "问净值"


def test_iter_file_sections_from_file_and_quote():
    body = {
        "msgtype": "file",
        "file": {"url": "https://example.com/f1", "aeskey": "fk1", "filename": "a.xlsx"},
        "quote": {
            "msgtype": "file",
            "file": {"url": "https://example.com/f2", "aeskey": "fk2"},
        },
    }
    sections = iter_file_sections(body)
    assert len(sections) == 2
    assert sections[0]["filename"] == "a.xlsx"
    assert sections[1]["aeskey"] == "fk2"


@pytest.mark.asyncio
async def test_download_files_as_attachments(tmp_path: Path):
    client = MagicMock()
    client.download_file = AsyncMock(return_value=(b"PK\x03\x04sheet", "rates.xlsx"))
    attachments = await download_files_as_attachments(
        client,
        [{"url": "https://x/file", "aeskey": "key", "filename": "hint.xlsx"}],
        dest_dir=tmp_path,
    )
    assert len(attachments) == 1
    att = attachments[0]
    assert att.type == AttachmentType.FILE
    assert att.local_path
    assert Path(att.local_path).read_bytes().startswith(b"PK")
    assert att.filename == "rates.xlsx"
    client.download_file.assert_awaited_once_with("https://x/file", "key")


@pytest.mark.asyncio
async def test_bridge_file_message_saves_and_stages(tmp_path: Path, monkeypatch):
    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="已收到文件")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream

    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.STUB),
    )
    settings = MagicMock()
    settings.wecom_stream_interval_ms = 50
    settings.wecom_welcome = ""

    bridge = WeComAgentBridge(
        main_agent=main_agent,
        settings=settings,
        message_gateway=gateway,
    )
    client = MagicMock()
    client.download_file = AsyncMock(return_value=(b"col1,col2\n1,2\n", "demo.csv"))
    client.reply_stream = AsyncMock()
    bridge._client = client

    monkeypatch.setattr(
        "csagent.wecom.bridge.upload_dir_for_session",
        lambda session_id, root=None: tmp_path / "session",
    )
    monkeypatch.setattr(
        "csagent.channels.message_gateway.stage_attachment",
        lambda att, session_id, index=0, update_pointer=True: _stage_into(
            att,
            session_id=session_id,
            index=index,
            root=tmp_path,
            update_pointer=update_pointer,
        ),
    )

    frame = {
        "body": {
            "msgtype": "file",
            "file": {"url": "https://corp/file", "aeskey": "fkey"},
            "from": {"userid": "u1"},
            "chattype": "single",
        }
    }
    await bridge._handle_inbound_message(frame)

    text = captured.get("text") or ""
    assert "工作区路径" in text or "demo.csv" in text or "附件" in text
    assert (tmp_path / "session" / "demo.csv").is_file() or "demo.csv" in text
    client.download_file.assert_awaited()
    client.reply_stream.assert_awaited()


def _stage_into(att, *, session_id, index, root, update_pointer=True):
    from csagent.channels.workspace_uploads import stage_attachment

    return stage_attachment(
        att,
        session_id=session_id,
        index=index,
        uploads_root=root / "uploads",
        project_root=root,
        update_pointer=update_pointer,
    )


def test_save_media_bytes_reuses_identical_file(tmp_path: Path):
    """同名同内容复用；同名不同内容仍加序号，绝不覆盖。"""
    first = save_media_bytes(b"same", dest_dir=tmp_path, filename="报告.xlsx")
    again = save_media_bytes(b"same", dest_dir=tmp_path, filename="报告.xlsx")
    assert again == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["报告.xlsx"]

    other = save_media_bytes(b"different", dest_dir=tmp_path, filename="报告.xlsx")
    assert other != first
    assert other.name == "报告_1.xlsx"
    assert first.read_bytes() == b"same"

    # 第三份内容与 _1 相同 → 复用 _1，而不是再开 _2
    third = save_media_bytes(b"different", dest_dir=tmp_path, filename="报告.xlsx")
    assert third == other
    assert sorted(p.name for p in tmp_path.iterdir()) == ["报告.xlsx", "报告_1.xlsx"]


@pytest.mark.asyncio
async def test_repeated_quote_of_same_file_keeps_one_copy(tmp_path: Path):
    """群聊里同一份文件被引用多次：会话目录只留一份，且指向同一路径。"""
    client = MagicMock()
    client.download_file = AsyncMock(return_value=(b"PK\x03\x04same", "报告.xlsx"))
    paths = []
    for _ in range(3):
        atts = await download_files_as_attachments(
            client,
            [{"url": "https://x/f", "aeskey": "k"}],
            dest_dir=tmp_path,
        )
        paths.append(atts[0].local_path)

    assert len(set(paths)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["报告.xlsx"]


def test_quote_file_without_url_is_reported_not_dropped():
    """引用机器人自己发的文件时只有 media_id、没有 url：必须显式暴露而非静默丢弃。"""
    body = {
        "msgtype": "text",
        "text": {"content": "看下这个文件"},
        "quote": {
            "msgtype": "file",
            "file": {"media_id": "MEDIA123", "filename": "机器人产出.xlsx"},
        },
    }
    sections = iter_file_sections(body)
    assert len(sections) == 1
    assert sections[0]["unavailable"] is True
    assert sections[0]["filename"] == "机器人产出.xlsx"


@pytest.mark.asyncio
async def test_unavailable_section_yields_failed_attachment_without_download(
    tmp_path: Path,
):
    """unavailable 段不发起下载，但要产出带 download_error、无 local_path 的附件。"""
    client = MagicMock()
    client.download_file = AsyncMock()
    attachments = await download_files_as_attachments(
        client,
        [{"unavailable": True, "reason": "没有可下载的 url", "filename": "x.xlsx"}],
        dest_dir=tmp_path,
    )
    assert len(attachments) == 1
    att = attachments[0]
    assert att.local_path is None
    assert att.metadata["download_error"] == "没有可下载的 url"
    client.download_file.assert_not_awaited()
