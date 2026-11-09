"""企微媒体下载与 Attachment 组装（图片/文件需 url+aeskey 解密后落盘）。

落盘目录：data/uploads/{session_id}/inbound/（用户上传，与 Agent outbound 隔离）。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import structlog

from csagent.channels.models import Attachment, AttachmentType
from csagent.channels.workspace_uploads import (
    DEFAULT_UPLOADS_ROOT,
    safe_filename,
    session_inbound_dir,
)

logger = structlog.get_logger(__name__)

# 与 workspace_uploads 共用，避免 wecom_uploads / uploads 双目录
DEFAULT_UPLOAD_ROOT = DEFAULT_UPLOADS_ROOT


def upload_dir_for_session(
    session_id: str,
    *,
    root: Path | None = None,
) -> Path:
    """用户上传落盘目录（data/uploads/{session}/inbound/）。"""
    return session_inbound_dir(session_id, root=root or DEFAULT_UPLOAD_ROOT)


def guess_image_filename(filename: str | None, *, index: int = 0) -> str:
    name = (filename or "").strip()
    if name and "." in Path(name).name:
        return Path(name).name
    return f"image_{index}.jpg"


def guess_file_filename(filename: str | None, *, index: int = 0) -> str:
    return safe_filename(filename, index=index, fallback_prefix="file")


def _same_content(path: Path, data: bytes) -> bool:
    """同名文件是否就是这份内容（先比长度，避免无谓整读）。"""
    try:
        if path.stat().st_size != len(data):
            return False
        return path.read_bytes() == data
    except OSError:
        return False


def save_media_bytes(
    data: bytes,
    *,
    dest_dir: Path,
    filename: str,
) -> Path:
    """写入本地文件并返回绝对路径。

    同名且内容一致时直接复用已有文件：群聊里文件只能靠引用抵达，用户每引用
    一次就会重新下载一遍，按名字加序号会在 inbound/ 里堆出 x_1、x_2……
    内容不同才让位（保留原有的加序号行为，绝不覆盖）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(guess_file_filename(filename)).name
    path = dest_dir / safe_name
    stem, suffix = path.stem, path.suffix
    n = 0
    while path.exists():
        if _same_content(path, data):
            logger.info("wecom_media_reused", path=str(path), size=len(data))
            return path.resolve()
        n += 1
        path = dest_dir / f"{stem}_{n}{suffix}"
    path.write_bytes(data)
    return path.resolve()


def image_info_from_body_section(section: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 body.image / item.image 取出 url/aeskey（缺 url 的处理同文件段）。"""
    if not isinstance(section, dict):
        return None
    url = str(section.get("url") or "").strip()
    if not url:
        return {
            "unavailable": True,
            "reason": "消息里没有可下载的 url（常见于引用机器人自己发出的图片）",
            "media_id": str(section.get("media_id") or "").strip() or None,
        }
    return {
        "url": url,
        "aeskey": str(section.get("aeskey") or section.get("aes_key") or "").strip() or None,
        "media_id": str(section.get("media_id") or "").strip() or None,
    }


def _mark_from_quote(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """标记该媒体段来自引用消息，供网关决定是否用它更新 .active_inbound。"""
    if info is not None:
        info["from_quote"] = True
    return info


def iter_image_sections(body: dict[str, Any]) -> list[dict[str, Any]]:
    """收集主消息与 quote 中的图片段（含 mixed 子项）。

    引用来源的段带 ``from_quote=True``：主消息自带附件时，引用里的旧文件
    不应抢走「当前用户文件」指针。
    """
    found: list[dict[str, Any]] = []
    msgtype = str(body.get("msgtype") or "").strip().lower()

    if msgtype == "image":
        info = image_info_from_body_section(body.get("image"))
        if info:
            found.append(info)
    elif msgtype == "mixed":
        items = (body.get("mixed") or {}).get("msg_item") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("msgtype") or "").strip().lower() != "image":
                    continue
                info = image_info_from_body_section(item.get("image"))
                if info:
                    found.append(info)

    quote = body.get("quote")
    if isinstance(quote, dict):
        qtype = str(quote.get("msgtype") or "").strip().lower()
        if qtype == "image":
            info = _mark_from_quote(image_info_from_body_section(quote.get("image")))
            if info:
                found.append(info)
        elif qtype == "mixed":
            items = (quote.get("mixed") or {}).get("msg_item") or []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("msgtype") or "").strip().lower() != "image":
                        continue
                    info = _mark_from_quote(
                        image_info_from_body_section(item.get("image"))
                    )
                    if info:
                        found.append(info)

    return found


def extract_plaintext_from_body(body: dict[str, Any], *, strip_at) -> str:
    """从 text / mixed / voice 抽出用户可读正文（不含引用拼装）。"""
    msgtype = str(body.get("msgtype") or "").strip().lower()
    if msgtype == "text":
        raw = str((body.get("text") or {}).get("content") or "").strip()
        return strip_at(raw)
    if msgtype == "voice":
        return str((body.get("voice") or {}).get("content") or "").strip()
    if msgtype == "mixed":
        parts: list[str] = []
        items = (body.get("mixed") or {}).get("msg_item") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").strip().lower()
                if item_type == "text":
                    content = str((item.get("text") or {}).get("content") or "").strip()
                    cleaned = strip_at(content)
                    if cleaned:
                        parts.append(cleaned)
        return "\n".join(parts).strip()
    if msgtype == "image":
        return ""
    if msgtype == "file":
        # 正文仅占位；真实文件由 download_files_as_attachments 落盘
        section = body.get("file") if isinstance(body.get("file"), dict) else {}
        name = str(
            section.get("filename") or section.get("name") or "文件"
        ).strip()
        return f"[用户发送了文件: {name}]"
    return ""


def file_info_from_body_section(section: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 body.file 取出 url/aeskey/filename。

    段存在但缺 url 时返回 ``unavailable`` 标记而非 None：静默丢掉会让 Agent
    沿用上一轮的「当前用户文件」指针，对着用户没提到的旧文件继续干活。
    """
    if not isinstance(section, dict):
        return None
    url = str(section.get("url") or "").strip()
    name = str(section.get("filename") or section.get("name") or "").strip() or None
    media_id = str(section.get("media_id") or "").strip() or None
    if not url:
        return {
            "unavailable": True,
            "reason": "消息里没有可下载的 url（常见于引用机器人自己发出的文件）",
            "filename": name,
            "media_id": media_id,
        }
    return {
        "url": url,
        "aeskey": str(section.get("aeskey") or section.get("aes_key") or "").strip() or None,
        "filename": name,
        "media_id": media_id,
    }


def iter_file_sections(body: dict[str, Any]) -> list[dict[str, Any]]:
    """收集主消息与 quote 中的文件段。

    引用来源的段带 ``from_quote=True``（含义见 :func:`iter_image_sections`）。
    群聊里文件只能经引用抵达，故引用段本身必须保留。
    """
    found: list[dict[str, Any]] = []
    msgtype = str(body.get("msgtype") or "").strip().lower()

    if msgtype == "file":
        info = file_info_from_body_section(body.get("file"))
        if info:
            found.append(info)

    quote = body.get("quote")
    if isinstance(quote, dict):
        qtype = str(quote.get("msgtype") or "").strip().lower()
        if qtype == "file":
            info = _mark_from_quote(file_info_from_body_section(quote.get("file")))
            if info:
                found.append(info)

    return found


def _guess_image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".gif", ".webp"}:
        return f"image/{suffix.lstrip('.')}"
    return "image/jpeg"


async def download_images_as_attachments(
    client: Any,
    image_sections: list[dict[str, Any]],
    *,
    dest_dir: Path,
) -> list[Attachment]:
    """调用 SDK download_file 解密落盘，生成图片 Attachment 列表。"""
    attachments: list[Attachment] = []
    for index, info in enumerate(image_sections):
        url = info.get("url") or ""
        aeskey = info.get("aeskey")
        from_quote = bool(info.get("from_quote"))
        if info.get("unavailable"):
            reason = str(info.get("reason") or "图片不可下载")
            logger.warning(
                "wecom_image_unavailable",
                reason=reason,
                from_quote=from_quote,
                has_media_id=bool(info.get("media_id")),
            )
            attachments.append(
                Attachment(
                    type=AttachmentType.IMAGE,
                    filename=guess_image_filename(None, index=index),
                    metadata={"download_error": reason, "from_quote": from_quote},
                )
            )
            continue
        try:
            buffer, filename = await client.download_file(url, aeskey)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wecom_image_download_failed",
                error=str(exc),
                has_aeskey=bool(aeskey),
                from_quote=from_quote,
            )
            attachments.append(
                Attachment(
                    type=AttachmentType.IMAGE,
                    filename=guess_image_filename(None, index=index),
                    url=url or None,
                    metadata={"download_error": str(exc), "from_quote": from_quote},
                )
            )
            continue

        if not buffer:
            logger.warning("wecom_image_empty_buffer", index=index)
            continue

        fname = guess_image_filename(filename, index=index)
        path = save_media_bytes(buffer, dest_dir=dest_dir, filename=fname)
        attachments.append(
            Attachment(
                type=AttachmentType.IMAGE,
                filename=fname,
                mime_type=_guess_image_mime(path),
                size_bytes=len(buffer),
                local_path=str(path),
                url=url or None,
                metadata={"aeskey_used": bool(aeskey), "from_quote": from_quote},
            )
        )
        logger.info(
            "wecom_image_saved",
            path=str(path),
            size=len(buffer),
            filename=fname,
        )
    return attachments


async def download_files_as_attachments(
    client: Any,
    file_sections: list[dict[str, Any]],
    *,
    dest_dir: Path,
) -> list[Attachment]:
    """调用 SDK download_file 解密落盘，生成文件 Attachment 列表。"""
    attachments: list[Attachment] = []
    for index, info in enumerate(file_sections):
        url = info.get("url") or ""
        aeskey = info.get("aeskey")
        hinted_name = info.get("filename")
        from_quote = bool(info.get("from_quote"))
        if info.get("unavailable"):
            reason = str(info.get("reason") or "文件不可下载")
            logger.warning(
                "wecom_file_unavailable",
                reason=reason,
                from_quote=from_quote,
                filename=hinted_name,
                has_media_id=bool(info.get("media_id")),
            )
            attachments.append(
                Attachment(
                    type=AttachmentType.FILE,
                    filename=guess_file_filename(hinted_name, index=index),
                    metadata={"download_error": reason, "from_quote": from_quote},
                )
            )
            continue
        try:
            buffer, filename = await client.download_file(url, aeskey)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wecom_file_download_failed",
                error=str(exc),
                has_aeskey=bool(aeskey),
                from_quote=from_quote,
            )
            attachments.append(
                Attachment(
                    type=AttachmentType.FILE,
                    filename=guess_file_filename(hinted_name, index=index),
                    url=url or None,
                    metadata={"download_error": str(exc), "from_quote": from_quote},
                )
            )
            continue

        if not buffer:
            logger.warning("wecom_file_empty_buffer", index=index)
            continue

        fname = guess_file_filename(filename or hinted_name, index=index)
        path = save_media_bytes(buffer, dest_dir=dest_dir, filename=fname)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attachments.append(
            Attachment(
                type=AttachmentType.FILE,
                filename=fname,
                mime_type=mime,
                size_bytes=len(buffer),
                local_path=str(path),
                url=url or None,
                metadata={"aeskey_used": bool(aeskey), "from_quote": from_quote},
            )
        )
        logger.info(
            "wecom_file_saved",
            path=str(path),
            size=len(buffer),
            filename=fname,
        )
    return attachments
