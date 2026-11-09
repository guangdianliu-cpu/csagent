"""channels 层基础单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.stream_events import AgentEventType, AgentStreamEvent


@pytest.mark.asyncio
async def test_message_gateway_forwards_to_main_agent():
    main_agent = MagicMock()

    async def _stream(**kwargs):
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="hello")

    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(main_agent=main_agent)

    msg = UnifiedMessage(
        channel="http_api",
        session_id="s1",
        text="你好",
    )
    events = [e async for e in gateway.handle(msg)]
    assert len(events) == 1
    assert events[0].content == "hello"


@pytest.mark.asyncio
async def test_message_gateway_tags_group_speaker():
    """群聊整群一个会话：给模型的正文和对话记录都标上发言人。"""
    main_agent = MagicMock()
    seen: dict = {}

    async def _stream(**kwargs):
        seen.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(main_agent=main_agent)

    msg = UnifiedMessage(
        channel="wecom",
        session_id="wecom:group:wrXXXX",
        text="接着刚才的问，费率多少",
        metadata={"speaker": "lisi"},
    )
    [_ async for _ in gateway.handle(msg)]
    assert seen["text"] == "[群成员 lisi] 接着刚才的问，费率多少"
    assert seen["transcript_user_text"] == "[群成员 lisi] 接着刚才的问，费率多少"


@pytest.mark.asyncio
async def test_message_gateway_no_speaker_tag_without_metadata():
    main_agent = MagicMock()
    seen: dict = {}

    async def _stream(**kwargs):
        seen.update(kwargs)
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent.handle_message_stream = _stream
    gateway = MessageGateway(main_agent=main_agent)
    msg = UnifiedMessage(channel="wecom", session_id="wecom:zhangsan", text="你好")
    [_ async for _ in gateway.handle(msg)]
    assert seen["text"] == "你好"
    assert seen["transcript_user_text"] == "你好"


@pytest.mark.asyncio
async def test_file_handler_image_stub():
    handler = FileHandler(image_mode=ImageMode.STUB)
    result = await handler.process(
        Attachment(type=AttachmentType.IMAGE, filename="a.png")
    )
    assert result.text is not None
    assert "stub" in result.text
    assert result.vision_parts == []
