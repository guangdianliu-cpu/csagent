"""审计证据：按内容寻址的不可变存档 + 文件摘要。

会话审计里工具参数只留 200 字预览，事后答不了「当时到底跑了哪段代码、发出去的是哪个文件」。
有副作用的工具（写脚本、跑脚本、写 / 发文件）经 ``record_state_change`` 登记证据：
文件给 sha256 与大小；脚本内容另存一份到 ``<审计目录>/blobs/<sha256 前两位>/<sha256>``，
同一内容只存一次、存了不再改。会话目录里的脚本可以被覆盖、被清理，存档不会。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# 单份存档上限：脚本正常不会这么大，超过的只记摘要不存全文
MAX_BLOB_BYTES = 2 * 1024 * 1024
# 文件摘要的读取上限：超过的大文件只按前这么多字节算摘要并注明
MAX_DIGEST_BYTES = 64 * 1024 * 1024

# 测试隔离用：非 None 时存档落这里，而不是 Settings 的审计目录
_blob_root_override: Path | None = None


def _blob_root() -> Path:
    if _blob_root_override is not None:
        return _blob_root_override
    from csagent.audit.session_logger import resolve_audit_dir
    from csagent.infra.config import get_settings

    return resolve_audit_dir(get_settings().audit_log_dir) / "blobs"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> dict[str, object]:
    """文件的 sha256 与大小；读失败返回带 error 的字典（证据缺失也要如实记下）。"""
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        remaining = MAX_DIGEST_BYTES
        with path.open("rb") as handle:
            while remaining > 0 and (chunk := handle.read(min(1024 * 1024, remaining))):
                digest.update(chunk)
                remaining -= len(chunk)
        info: dict[str, object] = {"sha256": digest.hexdigest(), "size": size}
        if size > MAX_DIGEST_BYTES:
            info["digest_scope"] = f"前 {MAX_DIGEST_BYTES} 字节"
        return info
    except OSError as exc:
        return {"error": str(exc)}


def store_blob(data: bytes) -> dict[str, object]:
    """存一份内容到存档，返回 ``{sha256, size, blob}``；超限或写失败时不带 blob。"""
    digest = sha256_bytes(data)
    info: dict[str, object] = {"sha256": digest, "size": len(data)}
    if len(data) > MAX_BLOB_BYTES:
        info["blob"] = ""
        info["blob_skipped"] = f"超过 {MAX_BLOB_BYTES} 字节，只记摘要"
        return info
    root = _blob_root()
    target = root / digest[:2] / digest
    try:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        info["blob"] = f"blobs/{digest[:2]}/{digest}"
    except OSError as exc:
        logger.error("evidence_blob_write_failed", sha256=digest, error=str(exc))
        info["blob"] = ""
    return info


def store_file_blob(path: Path) -> dict[str, object]:
    """把一个文件（脚本）存进存档；读失败返回带 error 的字典。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"error": str(exc)}
    return store_blob(data)


def record_bytes(kind: str, data: bytes, **fields: object) -> None:
    """把一段内容存档，并登记到本轮审计的 state_changes。

    没有请求上下文（单测直接调工具、离线脚本）时**存档也不写**：没有审计记录引用它，
    写了只会在审计目录里堆无主文件（测试曾因此往真实 data/audit/blobs/ 落文件）。
    """
    from csagent.audit.trace_buffer import get_audit_buffer, record_state_change

    if get_audit_buffer() is None:
        return
    record_state_change(kind, **store_blob(data), **fields)


def record_path(kind: str, file: Path, *, copy: bool = False, **fields: object) -> None:
    """登记一个已落盘文件。``copy=True`` 时另存一份不可变副本（脚本）；否则只记摘要（外发文件）。

    与 :func:`record_bytes` 相同：没有请求上下文时什么都不做。
    """
    from csagent.audit.trace_buffer import get_audit_buffer, record_state_change

    if get_audit_buffer() is None:
        return
    info = store_file_blob(file) if copy else file_digest(file)
    record_state_change(kind, **info, **fields)
