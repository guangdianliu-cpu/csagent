"""统一入站/出站消息模型（多通道归一化）。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AttachmentType(str, Enum):
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"
    VIDEO = "video"


class Attachment(BaseModel):
    """消息附件（图片/文件/语音等）。"""

    type: AttachmentType
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    # 本地临时路径（由 FileHandler 下载后填充）
    local_path: str | None = None
    # 原始 URL（来源通道提供）
    url: str | None = None
    # 附件处理后的文本描述（如图片 OCR 结果、语音转写文本）
    extracted_text: str | None = None
    # 附件元数据
    metadata: dict[str, Any] = Field(default_factory=dict)


class UnifiedMessage(BaseModel):
    """统一入站消息：所有通道适配后产出此对象。"""

    channel: str  # "http_api" / "wecom" 等
    session_id: str
    text: str = ""  # 消息正文
    attachments: list[Attachment] = Field(default_factory=list)
    quote_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 请求主体：user_id / auth_method / chat_type / chat_id，由通道从验签结果或
    # 平台回调里填，一路带到 CustomerScope（策略与审计用）
    principal: dict[str, str] = Field(default_factory=dict)


class UnifiedReply(BaseModel):
    """统一出站回复（非流式汇总场景）。"""

    text: str
    session_id: str
    channel: str
    attachments: list[Attachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
