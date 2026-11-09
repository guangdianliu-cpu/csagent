"""HTTP 聊天请求中的附件路径 → UnifiedMessage.attachments。"""

from __future__ import annotations

import mimetypes

import structlog

from csagent.channels.models import Attachment, AttachmentType
from csagent.infra.config import PROJECT_ROOT
from csagent.tools.packs.workspace_files_pack import (
    _DEFAULT_DENY_NAMES,
    is_denied_path,
    resolve_workspace_path,
)

logger = structlog.get_logger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def resolve_attachment_paths(
    paths: list[str],
) -> tuple[list[Attachment], list[dict[str, str]]]:
    """解析工作区相对路径，返回 (附件, 被丢弃的条目)。

    丢弃条目必须被上报——静默跳过会让「用户明明发了文件」变成
    「模型收到一段没有文件的文字然后开始猜」，是最难排查的一类故障。
    """
    result: list[Attachment] = []
    dropped: list[dict[str, str]] = []
    for index, raw in enumerate(paths or []):
        rel = str(raw or "").strip()
        if not rel:
            dropped.append({"path": str(raw), "reason": "空路径"})
            continue
        target = resolve_workspace_path(PROJECT_ROOT, rel, allow_absolute=False)
        if isinstance(target, str):
            dropped.append({"path": rel, "reason": f"路径非法: {target}"})
            continue
        if not target.is_file():
            dropped.append({"path": rel, "reason": "文件不存在（可能上传失败或会话不一致）"})
            continue
        if is_denied_path(target, set(_DEFAULT_DENY_NAMES)):
            dropped.append({"path": rel, "reason": "命中敏感文件拒绝清单"})
            continue
        suffix = target.suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            att_type = AttachmentType.IMAGE
        else:
            att_type = AttachmentType.FILE
        mime = mimetypes.guess_type(target.name)[0]
        try:
            size = target.stat().st_size
        except OSError:
            size = None
        result.append(
            Attachment(
                type=att_type,
                filename=target.name,
                mime_type=mime,
                size_bytes=size,
                local_path=str(target.resolve()),
                metadata={"workspace_path": rel, "source": "http_upload", "index": index},
            )
        )
    if dropped:
        logger.warning("http_attachment_dropped", dropped=dropped)
    return result, dropped


def attachments_from_workspace_paths(paths: list[str]) -> list[Attachment]:
    """兼容入口：只要附件，忽略丢弃明细。新代码请用 resolve_attachment_paths。"""
    attachments, _ = resolve_attachment_paths(paths)
    return attachments
