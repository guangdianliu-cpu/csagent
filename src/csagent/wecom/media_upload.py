"""企微长连接分片上传临时素材（官方 SDK 未封装，按协议自实现）。

流程：aibot_upload_media_init → chunk × N → finish → media_id
"""

from __future__ import annotations

import base64
import hashlib
import math
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from csagent.channels.outbound_files import detect_media_type

logger = structlog.get_logger(__name__)

_CHUNK_SIZE = 512 * 1024  # Base64 编码前
_MAX_CHUNKS = 100
_SIZE_LIMITS = {
    "image": 10 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}


def media_message_body(media_type: str, media_id: str) -> dict[str, Any]:
    """构造主动发送 / 被动回复用的消息体。"""
    key = media_type if media_type in {"image", "file", "voice", "video"} else "file"
    return {"msgtype": key, key: {"media_id": media_id}}


def _gen_req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


async def upload_media(
    client: Any,
    data: bytes,
    *,
    filename: str,
    media_type: str | None = None,
) -> str:
    """上传本地字节，返回 media_id。

    依赖官方 WSClient 的 ``_ws_manager.send_reply`` 等待回执。
    """
    if not data or len(data) < 5:
        raise ValueError("文件过小（至少 5 字节）")

    name = Path(filename or "file.bin").name
    mtype = (media_type or detect_media_type(name)).strip().lower() or "file"
    if mtype not in _SIZE_LIMITS:
        mtype = "file"
    limit = _SIZE_LIMITS[mtype]
    if len(data) > limit:
        raise ValueError(f"{mtype} 超过上限 {limit} bytes（当前 {len(data)}）")

    total_chunks = max(1, math.ceil(len(data) / _CHUNK_SIZE))
    if total_chunks > _MAX_CHUNKS:
        raise ValueError(f"分片数 {total_chunks} 超过上限 {_MAX_CHUNKS}")

    ws = getattr(client, "_ws_manager", None)
    if ws is None or not hasattr(ws, "send_reply"):
        raise RuntimeError("企微客户端不支持分片上传（缺少 _ws_manager.send_reply）")

    md5_hex = hashlib.md5(data).hexdigest()
    init_req = _gen_req_id("aibot_upload_media_init")
    init_ack = await ws.send_reply(
        init_req,
        {
            "type": mtype,
            "filename": name,
            "total_size": len(data),
            "total_chunks": total_chunks,
            "md5": md5_hex,
        },
        "aibot_upload_media_init",
    )
    upload_id = str((init_ack.get("body") or {}).get("upload_id") or "").strip()
    if not upload_id:
        raise RuntimeError(f"上传初始化未返回 upload_id: {init_ack}")

    for index in range(total_chunks):
        start = index * _CHUNK_SIZE
        chunk = data[start : start + _CHUNK_SIZE]
        chunk_req = _gen_req_id("aibot_upload_media_chunk")
        await ws.send_reply(
            chunk_req,
            {
                "upload_id": upload_id,
                "chunk_index": index,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            },
            "aibot_upload_media_chunk",
        )

    finish_req = _gen_req_id("aibot_upload_media_finish")
    finish_ack = await ws.send_reply(
        finish_req,
        {"upload_id": upload_id},
        "aibot_upload_media_finish",
    )
    media_id = str((finish_ack.get("body") or {}).get("media_id") or "").strip()
    if not media_id:
        raise RuntimeError(f"上传完成未返回 media_id: {finish_ack}")

    logger.info(
        "wecom_media_uploaded",
        filename=name,
        media_type=mtype,
        size=len(data),
        chunks=total_chunks,
        media_id=media_id[:16] + "...",
    )
    return media_id
