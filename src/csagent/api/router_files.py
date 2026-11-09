"""HTTP 文件上传 / 下载（工作区 data/uploads）。"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from csagent.channels.workspace_uploads import (
    DEFAULT_UPLOADS_ROOT,
    SCRIPTS_ROOT_REL,
    safe_filename,
    safe_session_segment,
    session_inbound_dir,
    set_active_inbound,
    workspace_relative,
)
from csagent.infra.auth import (
    AuthError,
    Principal,
    issue_download_grant,
    optional_principal,
    require_principal,
    verify_download_grant,
)
from csagent.api.file_preview import SHEET_SUFFIXES, sheet_preview
from csagent.audit.security_logger import record_security_event
from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.memory.artifact_store import register_artifact
from csagent.tools.visibility import is_downloadable_by_grant
from csagent.tools.packs.workspace_files_pack import (
    _DEFAULT_DENY_NAMES,
    is_denied_path,
    resolve_workspace_path,
)

router = APIRouter(prefix="/api/files", tags=["files"])

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class UploadResponse(BaseModel):
    workspace_path: str
    filename: str
    size_bytes: int
    download_url: str


class DownloadInfo(BaseModel):
    workspace_path: str
    filename: str
    download_url: str


def _require_http_channel(request: Request) -> None:
    manager = getattr(request.app.state, "channel_manager", None)
    if manager is None:
        return
    channel = manager.get("http_api")
    if channel is None or not getattr(channel, "is_running", False):
        raise HTTPException(status_code=503, detail="http_api channel disabled")


def _public_base_url() -> str:
    return (get_settings().public_base_url or "").strip().rstrip("/")


def build_download_url(workspace_path: str, *, session_id: str = "") -> str:
    """生成下载链接。

    链接自带一个路径级签名授权（``dl``），因为企微通道把它发到聊天里给用户点，
    那次请求带不了 Authorization 头。``session_id`` 参数保留只为兼容既有调用方，
    已不参与鉴权，也不再写进 URL（避免把会话标识泄进代理日志）。
    """
    _ = session_id
    q = f"path={quote(workspace_path, safe='')}"
    try:
        q += f"&dl={quote(issue_download_grant(workspace_path), safe='')}"
    except AuthError:
        # 鉴权关闭或密钥未配（本地开发）：退化成裸链接，由 download 端点放行
        pass
    path = f"api/files/download?{q}"
    base = _public_base_url()
    if base:
        # 企微把链接发到聊天里，必须是绝对地址。线上 PUBLIC_BASE_URL 要连前缀
        # 一起写，例如 https://lab.leadfund.com.cn/csagent
        return f"{base}/{path}"
    # 无 PUBLIC_BASE_URL：返回**相对**路径，跟着聊天页自身的 URL 走。
    # 写死 /api/... 的话，页面在 /csagent/ 下时会打到宿主域名根路径，nginx 不转发。
    return path


def _assert_owned_by(target: Path, session_id: str) -> None:
    """确认已解析的真实路径属于本会话。

    **必须传解析后的路径，不能拿请求里的原始字符串判前缀。**
    resolve_workspace_path() 只保证不越出项目根，并不拒绝 ``..``，所以
    ``data/uploads/web:me/../web:other/x.xlsx`` 的字符串前缀是自己的会话，
    解析后却落在别人目录里。按 Path.relative_to 判定才拦得住。

    会话文件横跨两棵树（见 workspace_uploads 模块注释）：
      data/uploads/{session}/...   上传与产出
      scripts/tools/{session}/...  会话脚本
    """
    seg = safe_session_segment(session_id)
    # 从 DEFAULT_UPLOADS_ROOT 推导而非拼死 "data/uploads"：上传落点就是它，
    # 两边必须同源，否则挪一次目录归属校验就会静默失效。
    roots = (
        (DEFAULT_UPLOADS_ROOT / seg).resolve(),
        (PROJECT_ROOT / SCRIPTS_ROOT_REL / seg).resolve(),
    )
    resolved = target.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    # 不区分「不存在」与「不属于你」，避免被拿来探测别人的文件名
    raise HTTPException(status_code=404, detail="文件不存在")


def resolve_downloadable_path(path_arg: str) -> Path:
    """解析可下载路径：须在项目根内且非敏感文件。"""
    target = resolve_workspace_path(PROJECT_ROOT, path_arg, allow_absolute=False)
    if isinstance(target, str):
        raise HTTPException(status_code=400, detail=target)
    if is_denied_path(target, set(_DEFAULT_DENY_NAMES)):
        raise HTTPException(status_code=403, detail="拒绝访问敏感文件")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_principal),
) -> UploadResponse:
    """上传文件到 data/uploads/{session_id}/inbound/，返回工作区相对路径。

    ``session_id`` 由验签身份派生，客户端不能指定——否则可以往别人的工作区投毒。
    """
    _require_http_channel(request)
    session_id = principal.session_id
    raw_name = file.filename or "upload.bin"
    fname = safe_filename(raw_name, fallback_prefix="upload")
    dest_dir = session_inbound_dir(session_id, root=DEFAULT_UPLOADS_ROOT)
    dest = dest_dir / fname
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1

    size = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过上限 {_MAX_UPLOAD_BYTES} bytes",
                    )
                handle.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"上传失败: {exc}") from exc
    finally:
        await file.close()

    rel = workspace_relative(dest)
    set_active_inbound(session_id, rel, root=DEFAULT_UPLOADS_ROOT)
    # 登记进产物清单：文件永久保留，事后「上个月传的那份还在吗」靠它回答。
    # 尽力而为，失败只告警——文件已经落盘，不该因为清单没记上而让上传失败。
    register_artifact(
        session_id=session_id,
        kind="inbound",
        path=rel,
        filename=dest.name,
        size=size,
    )
    return UploadResponse(
        workspace_path=rel,
        filename=dest.name,
        size_bytes=size,
        download_url=build_download_url(rel, session_id=session_id),
    )


@router.get("/download")
async def download_file(
    request: Request,
    path: str,
    dl: str = "",
    principal: Principal | None = Depends(optional_principal),
) -> FileResponse:
    """下载工作区文件（相对项目根路径）。

    两条通行方式，满足其一即可：

    1. ``dl`` 携带本服务签发的路径级授权——企微聊天里的链接走这条；
    2. ``Authorization: Bearer`` 身份令牌 + 归属校验——小窗直接调 API 走这条。

    两条都不满足则 401。查询串里的 session_id 已彻底失效。
    """
    return _file_response(_authorize_download(request, path, dl, principal))


class SheetPreview(BaseModel):
    name: str
    rows: list[list[str]]
    total_rows: int
    truncated: bool


class FilePreviewResponse(BaseModel):
    filename: str
    sheets: list[SheetPreview]


@router.get("/preview", response_model=FilePreviewResponse)
async def preview_file(
    request: Request,
    path: str,
    dl: str = "",
    principal: Principal | None = Depends(optional_principal),
) -> FilePreviewResponse:
    """表格文件（xlsx / csv）的预览：前几百行转成文字。鉴权与下载完全相同。"""
    target = _authorize_download(request, path, dl, principal)
    if target.suffix.lower() not in SHEET_SUFFIXES:
        raise HTTPException(status_code=415, detail="该格式暂不支持预览，请下载后查看")
    try:
        sheets = await asyncio.to_thread(sheet_preview, target)
    except Exception as exc:  # noqa: BLE001 - 坏文件、加密文件都只是「预览不了」
        raise HTTPException(status_code=422, detail="文件无法预览，请下载后查看") from exc
    return FilePreviewResponse(filename=target.name, sheets=[SheetPreview(**s) for s in sheets])


def _authorize_download(
    request: Request, path: str, dl: str, principal: Principal | None
) -> Path:
    """下载与预览共用的鉴权：返回可读的目标文件，不满足则抛 HTTPException。"""
    _require_http_channel(request)
    settings = get_settings()

    # 顺序很重要：**先鉴权，再解析路径**。反过来的话，未授权的人能靠
    # 400/404/403 的差别探测某个路径是否存在、是不是敏感文件。
    if not settings.agent_auth_enabled:
        return resolve_downloadable_path(path)

    if dl:
        try:
            granted = verify_download_grant(dl)
        except AuthError as exc:
            record_security_event(
                "download_via_grant", outcome="denied", request=request,
                path=path, reason=str(exc),
            )
            raise HTTPException(status_code=401, detail="下载链接无效或已过期") from exc
        target = resolve_downloadable_path(path)
        # 授权只对签发时那个路径有效：比对解析后的真实路径，防 ../ 串到别处
        if resolve_downloadable_path(granted).resolve() != target.resolve():
            record_security_event(
                "download_via_grant", outcome="denied", request=request,
                path=path, reason="链接与目标不符",
            )
            raise HTTPException(status_code=403, detail="下载链接与目标不符")
        rel = workspace_relative(target, project_root=PROJECT_ROOT)
        # 授权只该指向会话目录与 Skill 资源；伪造的授权取不到记忆库、审计、配置
        uploads_rel = workspace_relative(DEFAULT_UPLOADS_ROOT, project_root=PROJECT_ROOT)
        if not is_downloadable_by_grant(rel, uploads_rel=uploads_rel):
            record_security_event(
                "download_via_grant", outcome="denied", request=request,
                path=rel, reason="授权路径不在可下载范围内",
            )
            raise HTTPException(status_code=403, detail="下载链接与目标不符")
        # 链接不带身份、可被转发，谁凭它取走了什么必须留痕
        record_security_event("download_via_grant", request=request, path=rel)
        return target

    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="登录态无效或已过期，请在主产品中重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    target = resolve_downloadable_path(path)
    _assert_owned_by(target, principal.session_id)
    return target


def _file_response(target: Path) -> FileResponse:
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mime,
    )
