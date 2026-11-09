"""入口适配层：统一消息协议 + 通道适配。

生产入站路径：
  HTTP SSE / 企微 WS  →  MessageGateway  →  MainAgent
"""

from csagent.channels.file_handler import FileHandler, ImageMode
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage, UnifiedReply

__all__ = [
    "Attachment",
    "AttachmentType",
    "FileHandler",
    "ImageMode",
    "MessageGateway",
    "UnifiedMessage",
    "UnifiedReply",
]
