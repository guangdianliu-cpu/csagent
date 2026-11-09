"""文件/图片接收与预处理。

图片模式（ATTACHMENT_IMAGE_MODE / attachment.image_mode）：
- multimodal：不 OCR，把本地图/URL 编成 vision parts，交多模态 LLM 直接看图
- ocr：本地 OCR（RapidOCR / Tesseract，可选依赖）抽字注入文本
- stub：仅占位提示，不看图不抽字
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from csagent.channels.document_extract import extract_document_text
from csagent.channels.models import Attachment, AttachmentType
from csagent.channels.ocr import OcrBackend, create_default_ocr_backend

logger = structlog.get_logger(__name__)

_MAX_TEXT_CHARS = 8000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImageMode(str, Enum):
    MULTIMODAL = "multimodal"
    OCR = "ocr"
    STUB = "stub"

    @classmethod
    def parse(cls, value: str | None) -> "ImageMode":
        raw = (value or "").strip().lower()
        for item in cls:
            if item.value == raw:
                return item
        return cls.STUB


@dataclass
class AttachmentProcessResult:
    """单个附件的处理结果。"""

    text: str | None = None
    vision_parts: list[dict[str, Any]] = field(default_factory=list)


class FileHandler:
    """处理各类附件：文本/文档抽取，或为多模态 LLM 准备图片。"""

    def __init__(
        self,
        image_mode: ImageMode | str = ImageMode.STUB,
        ocr_backend: OcrBackend | None = None,
    ) -> None:
        self._image_mode = (
            image_mode if isinstance(image_mode, ImageMode) else ImageMode.parse(str(image_mode))
        )
        self._ocr = ocr_backend or create_default_ocr_backend()

    @property
    def image_mode(self) -> ImageMode:
        return self._image_mode

    async def process(self, attachment: Attachment) -> AttachmentProcessResult:
        if attachment.type == AttachmentType.IMAGE:
            return await self._process_image(attachment)
        if attachment.type == AttachmentType.FILE:
            return await self._process_file(attachment)
        if attachment.type == AttachmentType.VOICE:
            return AttachmentProcessResult(text=self._process_voice(attachment))
        return AttachmentProcessResult()

    async def _process_image(self, att: Attachment) -> AttachmentProcessResult:
        name = att.filename or "未命名图片"

        if att.extracted_text and self._image_mode != ImageMode.MULTIMODAL:
            return AttachmentProcessResult(text=att.extracted_text)

        if self._image_mode == ImageMode.MULTIMODAL:
            return self._process_image_multimodal(att, name)

        if self._image_mode == ImageMode.OCR:
            return await self._process_image_ocr(att, name)

        logger.info("file_handler_image_stub", filename=name, mode=self._image_mode.value)
        return AttachmentProcessResult(
            text=f"(图片 {name} 已接收，当前为 stub 模式，未启用看图/OCR)"
        )

    async def _process_image_ocr(self, att: Attachment, name: str) -> AttachmentProcessResult:
        """Step 1: 已有抽字 → Step 2: 本地 OCR → Step 3: 降级提示。"""
        if att.extracted_text:
            return AttachmentProcessResult(text=att.extracted_text)

        local = (att.local_path or "").strip()
        if not local:
            return AttachmentProcessResult(
                text=(
                    f"(图片 {name} 已接收；OCR 需要 local_path。"
                    "企微通道应先下载落盘，或改用 multimodal。)"
                )
            )
        path = Path(local)
        if not path.is_file():
            return AttachmentProcessResult(text=f"(图片 {name} 本地文件不存在: {local})")

        text = await asyncio.to_thread(self._ocr.extract_text, path)
        if text:
            clipped = text[:_MAX_TEXT_CHARS]
            suffix = "…(已截断)" if len(text) > _MAX_TEXT_CHARS else ""
            return AttachmentProcessResult(
                text=f"(图片 {name} OCR 结果)\n{clipped}{suffix}"
            )

        return AttachmentProcessResult(
            text=(
                f"(图片 {name} 已接收；OCR 未识别出文字或引擎未安装。"
                "可选: pip install 'cs-agent[ocr]'；"
                "或 ATTACHMENT_IMAGE_MODE=multimodal 由多模态模型看图。)"
            )
        )

    def _process_image_multimodal(self, att: Attachment, name: str) -> AttachmentProcessResult:
        part = self._build_vision_part(att)
        if part is None:
            return AttachmentProcessResult(
                text=(
                    f"(图片 {name} 已接收，但缺少 local_path/url，"
                    "无法交给多模态模型；请检查通道是否已下载附件。)"
                )
            )
        hint = f"(图片 {name} 已附带，将由多模态模型直接理解，未做 OCR)"
        if att.extracted_text:
            hint = f"{hint}\n附加说明: {att.extracted_text}"
        return AttachmentProcessResult(text=hint, vision_parts=[part])

    def _build_vision_part(self, att: Attachment) -> dict[str, Any] | None:
        local = (att.local_path or "").strip()
        if local:
            path = Path(local)
            if not path.is_file():
                logger.warning("file_handler_image_missing", path=local)
                return None
            try:
                raw = path.read_bytes()
            except OSError as exc:
                logger.warning("file_handler_image_read_failed", path=local, error=str(exc))
                return None
            if len(raw) > _MAX_IMAGE_BYTES:
                logger.warning(
                    "file_handler_image_too_large",
                    path=local,
                    size=len(raw),
                    limit=_MAX_IMAGE_BYTES,
                )
                return None
            mime = att.mime_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }

        url = (att.url or "").strip()
        if url.startswith(("http://", "https://", "data:")):
            return {"type": "image_url", "image_url": {"url": url}}
        return None

    async def _process_file(self, att: Attachment) -> AttachmentProcessResult:
        if att.extracted_text:
            return AttachmentProcessResult(text=att.extracted_text)

        name = att.filename or "未命名文件"
        local = (att.local_path or "").strip()
        if not local:
            return AttachmentProcessResult(
                text=f"(文件 {name} 已接收，但缺少 local_path，无法解析内容)"
            )

        path = Path(local)
        if not path.is_file():
            return AttachmentProcessResult(text=f"(文件 {name} 本地不存在: {local})")

        text, detail = await asyncio.to_thread(
            extract_document_text, path, max_chars=_MAX_TEXT_CHARS
        )
        if text:
            return AttachmentProcessResult(
                text=f"(文件 {name} 文本内容，引擎={detail})\n{text}"
            )
        return AttachmentProcessResult(
            text=f"(文件 {name} 无法解析：{detail})"
        )

    @staticmethod
    def _process_voice(att: Attachment) -> str | None:
        return att.extracted_text
