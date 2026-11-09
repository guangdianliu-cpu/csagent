"""会话级出站文件队列：Agent 调用 send_file 入队，通道在回复结束后发送。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import structlog

logger = structlog.get_logger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
_VIDEO_SUFFIXES = {".mp4"}
_VOICE_SUFFIXES = {".amr"}


def detect_media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix in _VOICE_SUFFIXES:
        return "voice"
    return "file"


@dataclass(frozen=True)
class OutboundFile:
    """待发送的工作区文件。"""

    path: str  # 绝对路径
    workspace_path: str  # 相对项目根
    filename: str
    media_type: str  # file | image | voice | video


_lock = Lock()
_queues: dict[str, list[OutboundFile]] = {}


def enqueue_outbound_file(session_id: str, item: OutboundFile) -> int:
    """入队；返回当前会话队列长度。"""
    key = (session_id or "").strip() or "unknown"
    with _lock:
        bucket = _queues.setdefault(key, [])
        bucket.append(item)
        size = len(bucket)
    logger.info(
        "outbound_file_enqueued",
        session_id=key,
        workspace_path=item.workspace_path,
        media_type=item.media_type,
        queue_size=size,
    )
    return size


def drain_outbound_files(session_id: str) -> list[OutboundFile]:
    """取出并清空该会话的出站队列。"""
    key = (session_id or "").strip() or "unknown"
    with _lock:
        items = _queues.pop(key, [])
    if items:
        logger.info("outbound_files_drained", session_id=key, count=len(items))
    return items


def peek_outbound_files(session_id: str) -> list[OutboundFile]:
    key = (session_id or "").strip() or "unknown"
    with _lock:
        return list(_queues.get(key, []))


def clear_outbound_files(session_id: str | None = None) -> None:
    """测试用：清空指定会话或全部队列。"""
    with _lock:
        if session_id is None:
            _queues.clear()
        else:
            _queues.pop((session_id or "").strip() or "unknown", None)
