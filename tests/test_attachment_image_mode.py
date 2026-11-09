"""附件图片模式：multimodal / ocr / stub。"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.react.loop import ReactLoop
from csagent.react.models import CustomerScope, DomainTaskRequest


def _tiny_png(path: Path) -> Path:
    # 1x1 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    path.write_bytes(raw)
    return path


@pytest.mark.asyncio
async def test_multimodal_builds_vision_part(tmp_path: Path):
    img = _tiny_png(tmp_path / "a.png")
    handler = FileHandler(image_mode="multimodal")
    result = await handler.process(
        Attachment(
            type=AttachmentType.IMAGE,
            filename="a.png",
            local_path=str(img),
            mime_type="image/png",
        )
    )
    assert result.text is not None
    assert "多模态" in result.text
    assert len(result.vision_parts) == 1
    part = result.vision_parts[0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_ocr_mode_uses_extracted_text():
    handler = FileHandler(image_mode=ImageMode.OCR)
    result = await handler.process(
        Attachment(
            type=AttachmentType.IMAGE,
            filename="x.png",
            extracted_text="费率表文字",
        )
    )
    assert result.text == "费率表文字"
    assert result.vision_parts == []


@pytest.mark.asyncio
async def test_ocr_mode_without_text_degrades():
    handler = FileHandler(image_mode="ocr")
    result = await handler.process(
        Attachment(type=AttachmentType.IMAGE, filename="x.png")
    )
    assert result.text is not None
    assert "OCR" in result.text
    assert result.vision_parts == []


@pytest.mark.asyncio
async def test_gateway_passes_vision_parts_to_main_agent(tmp_path: Path):
    img = _tiny_png(tmp_path / "shot.png")
    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(
        main_agent=main_agent,
        file_handler=FileHandler(image_mode=ImageMode.MULTIMODAL),
    )
    msg = UnifiedMessage(
        channel="http_api",
        session_id="s1",
        text="这张图里费率多少",
        attachments=[
            Attachment(
                type=AttachmentType.IMAGE,
                filename="shot.png",
                local_path=str(img),
            )
        ],
    )
    _ = [e async for e in gateway.handle(msg)]
    assert captured.get("vision_parts")
    assert captured["vision_parts"][0]["type"] == "image_url"
    assert "费率" in captured["text"]


def test_react_loop_init_messages_embeds_vision_parts():
    loop = ReactLoop(
        llm_client=MagicMock(),
        tool_registry=MagicMock(),
    )
    request = DomainTaskRequest(
        task_id="t1",
        domain="product",
        instruction="看图回答",
        customer_scope=CustomerScope(session_id="s1"),
        trace_id="tr",
        vision_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
        ],
    )
    messages = loop._init_messages("sys", request)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "看图回答" in content[0]["text"]
    assert content[1]["type"] == "image_url"
