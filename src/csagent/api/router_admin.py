"""管理后台接口：管理员登录、数据集状态、上传发布。

鉴权与聊天接口完全分开：
- 管理员账号来自 ``ADMIN_ACCOUNTS``（用户名 + PBKDF2 口令哈希），登录换一个
  ``typ="adm"`` 的短时令牌，用只在本服务的 ``ADMIN_TOKEN_SECRET`` 签名；
- 管理令牌换不到聊天接口，宿主签发的身份令牌也进不了管理接口。

登录、签调试令牌、发布数据集、改图标等越过普通用户权限的动作都写安全审计
（``data/audit/security-*.jsonl``，见 audit/security_logger.py），不跟随 LOG_LEVEL。

``AGENT_AUTH_ENABLED=false``（仅本地开发）时管理接口同样放行，与其它接口口径一致。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError

from csagent.admin import branding, quick_entries
from csagent.admin.datasets import STAGING_DIR, DatasetPublisher
from csagent.admin.passwd import parse_accounts, verify_password
from csagent.audit.security_logger import record_security_event
from csagent.infra.auth import (
    DEBUG_UID_PREFIX,
    AuthError,
    Principal,
    extract_token,
    issue_token,
    verify_admin_token,
)
from csagent.infra.config import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 登录失败限流：同一用户名连续失败 N 次后锁定一段时间（进程内）
_MAX_FAILURES = 5
_LOCK_SECONDS = 300
_failures: dict[str, tuple[int, float]] = {}

_CHUNK = 1024 * 1024
# 单批最多文件数（覆盖一个数据集整期的全部源文件，留点余量）
_MAX_BATCH_FILES = 20


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_in: int
    username: str


async def require_admin(request: Request) -> Principal:
    settings = get_settings()
    if not settings.agent_auth_enabled:
        return Principal(user_id="dev-admin", name="本地开发")
    token = extract_token(request)
    try:
        principal = verify_admin_token(token)
    except AuthError as exc:
        # 带了令牌却验不过（伪造、过期、拿身份令牌冒充）才记；没带令牌的只是未登录
        if token:
            record_security_event(
                "admin_auth_rejected",
                outcome="denied",
                request=request,
                path=request.url.path,
                reason=str(exc),
            )
        raise HTTPException(
            status_code=401,
            detail="管理登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # 账号被从 ADMIN_ACCOUNTS 删掉后，未过期的令牌也立即失效
    if principal.user_id not in parse_accounts(settings.admin_accounts):
        record_security_event(
            "admin_auth_rejected",
            outcome="denied",
            actor=principal.user_id,
            request=request,
            path=request.url.path,
            reason="账号已停用",
        )
        raise HTTPException(status_code=401, detail="管理账号已停用")
    return principal


def _publisher(request: Request) -> DatasetPublisher:
    publisher = getattr(request.app.state, "dataset_publisher", None)
    if publisher is None:
        publisher = DatasetPublisher()
        request.app.state.dataset_publisher = publisher
    return publisher


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    settings = get_settings()
    username = body.username.strip()
    now = time.time()
    count, locked_until = _failures.get(username, (0, 0.0))
    if locked_until > now:
        record_security_event(
            "admin_login", outcome="locked", actor=username, request=request
        )
        raise HTTPException(
            status_code=429,
            detail=f"失败次数过多，请 {int(locked_until - now) + 1} 秒后再试",
        )

    encoded = parse_accounts(settings.admin_accounts).get(username)
    if not encoded or not verify_password(body.password, encoded):
        count += 1
        _failures[username] = (
            (0, now + _LOCK_SECONDS) if count >= _MAX_FAILURES else (count, 0.0)
        )
        logger.warning("admin_login_failed", username=username, failures=count)
        record_security_event(
            "admin_login",
            outcome="failure",
            actor=username,
            request=request,
            failures=count,
            locked=count >= _MAX_FAILURES,
        )
        raise HTTPException(status_code=401, detail="用户名或口令错误")

    _failures.pop(username, None)
    ttl = int(settings.admin_token_ttl_seconds)
    try:
        token = issue_token(username, name=username, ttl_seconds=ttl, typ="adm")
    except AuthError as exc:
        # 口令对了但签不出令牌：ADMIN_TOKEN_SECRET 缺失 / 过短 / 与共享密钥相同
        logger.error("admin_token_issue_failed", username=username, error=str(exc))
        record_security_event(
            "admin_login", outcome="failure", actor=username, request=request,
            reason=f"管理令牌密钥不可用: {exc}",
        )
        raise HTTPException(
            status_code=503, detail=f"管理后台未完成配置：{exc}"
        ) from exc
    logger.info("admin_login", username=username)
    record_security_event("admin_login", actor=username, request=request, ttl_seconds=ttl)
    return LoginResponse(token=token, expires_in=ttl, username=username)


@router.get("/me")
async def me(principal: Principal = Depends(require_admin)) -> dict:
    return {"username": principal.user_id}


# 调试令牌上限：够一次联调，泄露了也很快作废（令牌无法吊销，只能等过期）
_DEBUG_TOKEN_MAX_TTL = 2 * 3600
# 调试令牌的 uid 一律落在 DEBUG_UID_PREFIX 命名空间下：调试会话与真实用户的会话、
# 记忆、文件永远不重叠，管理员拿它冒充不了任何真实用户


class DebugTokenRequest(BaseModel):
    user_id: str = Field(default="local_user", min_length=1, max_length=64)
    name: str = Field(default="本地调试", max_length=64)
    ttl_seconds: int = Field(default=_DEBUG_TOKEN_MAX_TTL, ge=60, le=_DEBUG_TOKEN_MAX_TTL)


def debug_uid(raw: str) -> str:
    """调试令牌的 uid：统一补上 ``debug:`` 前缀（已有则不重复加）。"""
    uid = raw.strip()
    return uid if uid.startswith(DEBUG_UID_PREFIX) else f"{DEBUG_UID_PREFIX}{uid}"


@router.post("/debug-token")
async def debug_token(
    body: DebugTokenRequest, request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    """给小窗调试页（/debug）签一个短时身份令牌。

    以前调试页把 30 天令牌写死在 HTML 里、放在公网上，谁打开谁就能用；
    现在页面不带任何令牌，由管理员登录后现签，每次签发都写安全审计。
    uid 固定带 ``debug:`` 前缀，只能进调试会话，不能冒充真实用户读其历史与文件。
    """
    uid = debug_uid(body.user_id)
    try:
        token = issue_token(uid, name=body.name.strip(), ttl_seconds=body.ttl_seconds)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=f"签发失败: {exc}") from exc
    logger.warning(
        "admin_debug_token_issued",
        admin=principal.user_id,
        user_id=uid,
        ttl_seconds=body.ttl_seconds,
    )
    record_security_event(
        "admin_debug_token_issued",
        actor=principal.user_id,
        request=request,
        target_uid=uid,
        ttl_seconds=body.ttl_seconds,
    )
    return {"token": token, "expires_in": body.ttl_seconds, "user_id": uid}


@router.get("/datasets")
async def list_datasets(
    request: Request, _: Principal = Depends(require_admin)
) -> dict:
    publisher = _publisher(request)
    items = []
    for spec in publisher.specs.values():
        running = [j for j in publisher.jobs_for(spec.id) if j.status in ("queued", "running")]
        items.append({
            **spec.public(),
            "state": await publisher.describe(spec),
            "running_jobs": [j.public() for j in running],
        })
    return {"datasets": items}


async def _stage(file: UploadFile, spec, filename: str) -> tuple[Path, int, str]:
    """把一个上传文件流式落到暂存区；超限 / 空文件抛 HTTPException 且不留残片。

    :returns: ``(暂存路径, 字节数, sha256)``；后两者写进安全审计，事后能核对
        「线上数据是哪一份文件发布的」
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged = STAGING_DIR / f"{uuid.uuid4().hex}{Path(filename).suffix.lower()}"
    size = 0
    digest = hashlib.sha256()
    try:
        with staged.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > spec.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{filename} 超过 {spec.max_upload_bytes // (1024 * 1024)} MB 上限",
                    )
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail=f"{filename} 是空文件")
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return staged, size, digest.hexdigest()


@router.post("/datasets/{dataset_id}/upload")
async def upload_dataset_files(
    request: Request,
    dataset_id: str,
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
    principal: Principal = Depends(require_admin),
) -> dict:
    """上传并发布一个或多个文件（表单字段 ``files`` 可重复；兼容单个 ``file``）。

    整批先全部落盘并做基本校验（扩展名、大小、同批重名），任何一个不合格
    整批拒绝、一个都不发布；通过后每个文件各登记一条发布任务，按顺序串行执行。
    各文件是否匹配数据集、sheet 是否齐全由 builder 在各自任务里判定，
    某个文件失败不影响同批其它文件。
    """
    publisher = _publisher(request)
    spec = publisher.get(dataset_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="数据集不存在")

    uploads = list(files or []) + ([file] if file is not None else [])
    if not uploads:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if len(uploads) > _MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {_MAX_BATCH_FILES} 个文件")

    names = [Path(u.filename or "").name for u in uploads]
    for name in names:
        if not name or Path(name).suffix.lower() not in spec.accept:
            raise HTTPException(
                status_code=400,
                detail=f"{name or '（无文件名）'}：只接受 {', '.join(spec.accept)} 文件",
            )
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise HTTPException(status_code=400, detail=f"同一批里有重名文件：{', '.join(dup)}")

    staged: list[Path] = []
    files_meta: list[dict] = []
    try:
        for upload, name in zip(uploads, names):
            path, size, sha256 = await _stage(upload, spec, name)
            staged.append(path)
            files_meta.append({"name": name, "size": size, "sha256": sha256})
    except BaseException:
        for path in staged:
            path.unlink(missing_ok=True)
        raise

    jobs = [
        publisher.submit(spec, path, name, principal.user_id).public()
        for path, name in zip(staged, names)
    ]
    for meta, job in zip(files_meta, jobs):
        meta["job_id"] = job.get("id")
    record_security_event(
        "dataset_upload_submitted",
        actor=principal.user_id,
        request=request,
        dataset_id=dataset_id,
        files=files_meta,
    )
    return {"jobs": jobs, "job": jobs[0]}


@router.get("/jobs/{job_id}")
async def get_job(
    request: Request, job_id: str, _: Principal = Depends(require_admin)
) -> dict:
    job = _publisher(request).job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启后任务记录会清空）")
    return {"job": job.public()}


@router.get("/datasets/{dataset_id}/jobs")
async def list_jobs(
    request: Request, dataset_id: str, _: Principal = Depends(require_admin)
) -> dict:
    return {"jobs": [j.public() for j in _publisher(request).jobs_for(dataset_id)]}


# ---------------------------------------------------------------- 悬浮图标


class BrandingModeRequest(BaseModel):
    icon_mode: str = Field(..., max_length=16)


@router.get("/branding")
async def get_branding(_: Principal = Depends(require_admin)) -> dict:
    return {"branding": branding.load_state().public(), "modes": list(branding.ICON_MODES)}


@router.post("/branding/icon")
async def upload_branding_icon(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_admin),
) -> dict:
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in branding.ICON_TYPES:
        raise HTTPException(
            status_code=400, detail=f"只接受 {', '.join(branding.ICON_TYPES)} 图片"
        )
    try:
        data = await file.read(branding.MAX_ICON_BYTES + 1)
    finally:
        await file.close()
    if len(data) > branding.MAX_ICON_BYTES:
        raise HTTPException(
            status_code=413, detail=f"图标不能超过 {branding.MAX_ICON_BYTES // 1024} KB"
        )
    if not data or not branding.looks_like_image(data, ext):
        raise HTTPException(status_code=400, detail="文件内容不是有效的图片")
    state = branding.save_icon(data, ext)
    logger.info("branding_icon_updated", operator=principal.user_id, file=state.icon_file)
    record_security_event(
        "branding_icon_updated",
        actor=principal.user_id,
        request=request,
        file=state.icon_file,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return {"branding": state.public()}


@router.put("/branding")
async def update_branding(
    body: BrandingModeRequest, request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    try:
        state = branding.set_mode(body.icon_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("branding_mode_updated", operator=principal.user_id, mode=state.icon_mode)
    record_security_event(
        "branding_mode_updated", actor=principal.user_id, request=request, mode=state.icon_mode
    )
    return {"branding": state.public()}


@router.delete("/branding/icon")
async def reset_branding_icon(
    request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    state = branding.reset_icon()
    logger.info("branding_icon_reset", operator=principal.user_id)
    record_security_event("branding_icon_reset", actor=principal.user_id, request=request)
    return {"branding": state.public()}


# ---------------------------------------------------------------- 快捷入口


class QuickEntriesSaveRequest(BaseModel):
    # 编辑时看到的版本；与当前不一致说明别人先保存了，拒绝覆盖
    base_version: int
    # 不直接用 QuickEntries 当类型：校验失败时 FastAPI 回的是一串结构化错误，
    # 后台页面只会显示成 [object Object]。这里自己校验、拼成一句话
    entries: dict


class QuickEntriesRollbackRequest(BaseModel):
    version: int


def _quick_entries_state() -> dict:
    loaded = quick_entries.load()
    return {
        "source": loaded.source,
        "version": loaded.version,
        "updated_by": loaded.updated_by,
        "updated_at": loaded.updated_at,
        "entries": loaded.entries.model_dump(mode="json") if loaded.entries else None,
        "history": quick_entries.history(),
    }


# 校验报错的字段路径 → 后台表单上的叫法
_QE_FIELD_NAMES = {
    "welcome": "欢迎区",
    "title": "欢迎标题",
    "subtitle": "副标题",
    "placeholder": "输入框提示",
    "capability_intro": "「能查什么」说明",
    "groups": "第 {} 组",
    "categories": "第 {} 个分类",
    "tags": "第 {} 个能力胶囊",
    "questions": "问题",
    "keywords": "关键词",
    "label": "名称",
    "key": "key",
    "icon": "图标",
    "entity": "关键词联想",
}


def _readable_loc(loc: tuple) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if parts and "{}" in parts[-1]:
                parts[-1] = parts[-1].format(item + 1)
            continue
        parts.append(_QE_FIELD_NAMES.get(str(item), str(item)))
    # 列表本身出错（如超长）时没有下标，去掉占位
    return " › ".join(p.replace("第 {} ", "") for p in parts)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        where = _readable_loc(tuple(err.get("loc", ())))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "；".join(parts)


@router.get("/quick-entries")
async def get_quick_entries(_: Principal = Depends(require_admin)) -> dict:
    return _quick_entries_state()


@router.put("/quick-entries")
async def save_quick_entries(
    body: QuickEntriesSaveRequest, request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    try:
        entries = quick_entries.QuickEntries.model_validate(body.entries)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_message(exc)) from exc
    try:
        loaded = quick_entries.save(entries, principal.user_id, base_version=body.base_version)
    except quick_entries.VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    content = json.dumps(body.entries, ensure_ascii=False, sort_keys=True).encode()
    logger.info("quick_entries_saved", operator=principal.user_id, version=loaded.version)
    record_security_event(
        "quick_entries_saved",
        actor=principal.user_id,
        request=request,
        version=loaded.version,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return _quick_entries_state()


@router.post("/quick-entries/rollback")
async def rollback_quick_entries(
    body: QuickEntriesRollbackRequest, request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    try:
        loaded = quick_entries.rollback(body.version, principal.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("quick_entries_rollback", operator=principal.user_id, to=body.version)
    record_security_event(
        "quick_entries_rollback",
        actor=principal.user_id,
        request=request,
        to_version=body.version,
        version=loaded.version,
    )
    return _quick_entries_state()


@router.delete("/quick-entries")
async def reset_quick_entries(
    request: Request, principal: Principal = Depends(require_admin)
) -> dict:
    quick_entries.reset()
    logger.info("quick_entries_reset", operator=principal.user_id)
    record_security_event("quick_entries_reset", actor=principal.user_id, request=request)
    return _quick_entries_state()
