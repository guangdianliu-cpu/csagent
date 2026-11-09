"""宿主签发令牌的签发、验签与身份派生。

方案：宿主服务端与本服务共享一把长密钥（``AGENT_SHARED_SECRET``）。宿主在自己
的后端用这把密钥对 ``{uid, exp}`` 做 HMAC-SHA256 签名，把令牌交给它自己的前端；
前端经 postMessage 把令牌下发给小窗，小窗每次请求带上；本服务用同一把密钥验签。

**密钥只存在于两边的服务端，永不下发浏览器。** 浏览器只拿得到一个短时效、
只对单个用户有效的令牌，拿不到伪造其他用户令牌的能力。

令牌格式（紧凑，无第三方依赖）::

    v1.<base64url(payload_json)>.<base64url(hmac_sha256)>

payload 字段：

======  ====  ============================================================
uid     必填  宿主侧稳定用户主键；``session_id`` 由它派生为 ``web:{uid}``
exp     必填  Unix 秒，过期即拒
iat     选填  签发时间，仅用于排查
name    选填  展示名，仅用于界面
======  ====  ============================================================

签名覆盖 ``v1.<payload_b64>`` 整串（含版本前缀），因此换版本即换签名域，
老令牌不会在新版本下意外通过。算法固定为 HMAC-SHA256，令牌里不带 alg 字段，
从结构上排除了 JWT 那类「alg 混淆 / alg:none」攻击。

**管理令牌（typ="adm"）用另一把只在本服务的密钥** ``ADMIN_TOKEN_SECRET`` 签名。
共享密钥在宿主服务端也有一份，若管理令牌也用它签，宿主后端就能给任意已知的
管理员用户名签出管理令牌——身份认证与管理授权不能是同一张「万能令牌」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from csagent.infra.config import get_settings

_VERSION = "v1"
# 防止超大 payload 拖垮解析；正常令牌 200 字节以内
_MAX_TOKEN_BYTES = 4096
# 容忍宿主与本机之间的时钟漂移
_CLOCK_SKEW_SECONDS = 60
# 调试令牌的 uid 命名空间（管理后台 /debug-token 签发）：与真实用户永不重叠
DEBUG_UID_PREFIX = "debug:"
# 低于此长度的密钥直接判为配置错误：HMAC-SHA256 的密钥应当有足够熵
MIN_SECRET_LENGTH = 32


class AuthError(Exception):
    """令牌不合法。消息只用于服务端日志，不原样回给调用方。"""


@dataclass(frozen=True)
class Principal:
    """验签通过后的可信身份。"""

    user_id: str
    name: str = ""
    expires_at: int = 0
    # host_token（宿主签发）| debug_token（管理员给调试页签的 debug: 命名空间）|
    # anonymous（AGENT_AUTH_ENABLED=false 的本地开发）。审计要分得清是谁在用哪种通行方式。
    auth_method: str = "host_token"

    @property
    def session_id(self) -> str:
        """会话/记忆边界。口径与企微侧 ``wecom:{from_user}`` 一致。"""
        return f"web:{self.user_id}"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(signing_input: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64encode(mac.digest())


def resolve_secret(secret: str | None = None) -> str:
    """取密钥并校验强度。配置缺失/过短一律抛错，绝不静默放行。"""
    value = (secret if secret is not None else get_settings().agent_shared_secret).strip()
    if not value:
        raise AuthError("AGENT_SHARED_SECRET 未配置")
    if len(value) < MIN_SECRET_LENGTH:
        raise AuthError(
            f"AGENT_SHARED_SECRET 过短（{len(value)} < {MIN_SECRET_LENGTH}）"
        )
    return value


def resolve_admin_secret(secret: str | None = None) -> str:
    """取管理令牌密钥（``ADMIN_TOKEN_SECRET``）并校验。

    管理令牌只由本服务签发、只给本服务验，所以密钥绝不能是给宿主的那把
    ``AGENT_SHARED_SECRET``——否则宿主后端就能给任意已知管理员用户名签出管理令牌。
    缺失、过短、与共享密钥相同一律抛错。
    """
    settings = get_settings()
    value = (secret if secret is not None else settings.admin_token_secret).strip()
    if not value:
        raise AuthError("ADMIN_TOKEN_SECRET 未配置")
    if len(value) < MIN_SECRET_LENGTH:
        raise AuthError(
            f"ADMIN_TOKEN_SECRET 过短（{len(value)} < {MIN_SECRET_LENGTH}）"
        )
    shared = str(settings.agent_shared_secret or "").strip()
    if shared and hmac.compare_digest(value.encode("utf-8"), shared.encode("utf-8")):
        raise AuthError("ADMIN_TOKEN_SECRET 不能与 AGENT_SHARED_SECRET 相同")
    return value


def _key_for(typ: str | None, secret: str | None) -> str:
    """按令牌类型取签名密钥：管理令牌用独立密钥，其余用与宿主共享的密钥。"""
    if typ == "adm":
        return resolve_admin_secret(secret)
    return resolve_secret(secret)


def issue_token(
    user_id: str,
    *,
    name: str = "",
    ttl_seconds: int | None = None,
    secret: str | None = None,
    typ: str = "id",
) -> str:
    """签发令牌。

    生产环境由**宿主服务端**签发，本函数仅供联调、自测与 dev-token 端点使用；
    对方若用 Java/Go 实现，照 docs/host-integration.md 的算法即可，无需调用这里。

    ``typ="adm"`` 签的是管理后台令牌：``verify_token`` 不认它，它也换不到聊天接口；
    它用 ``ADMIN_TOKEN_SECRET`` 签名（见 :func:`resolve_admin_secret`），``secret``
    参数此时指的也是这把管理密钥。
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise AuthError("user_id 不能为空")
    key = _key_for(typ, secret)
    ttl = ttl_seconds if ttl_seconds is not None else get_settings().agent_token_ttl_seconds
    now = int(time.time())
    payload = {"typ": typ, "uid": uid, "iat": now, "exp": now + int(ttl)}
    if name:
        payload["name"] = str(name)
    body = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{_VERSION}.{body}"
    return f"{signing_input}.{_sign(signing_input, key)}"


def verify_token(token: str, *, secret: str | None = None) -> Principal:
    """验签并返回可信身份；任何一步不通过都抛 :class:`AuthError`。"""
    return _verify_typed(token, secret=secret, accepted=(None, "id"))


def verify_admin_token(token: str, *, secret: str | None = None) -> Principal:
    """验签管理后台令牌（``typ="adm"``，用 ``ADMIN_TOKEN_SECRET`` 验）。

    身份令牌、下载授权、拿共享密钥签出来的 adm 令牌一律拒绝。
    """
    return _verify_typed(token, secret=secret, accepted=("adm",))


def _verify_typed(
    token: str, *, secret: str | None, accepted: tuple[str | None, ...]
) -> Principal:
    raw = (token or "").strip()
    if not raw:
        raise AuthError("令牌为空")
    if len(raw.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise AuthError("令牌过长")

    parts = raw.split(".")
    if len(parts) != 3:
        raise AuthError("令牌格式错误")
    version, body, signature = parts
    if version != _VERSION:
        raise AuthError(f"令牌版本不支持: {version}")

    # 管理接口只认管理密钥签的令牌：拿共享密钥签出来的 adm 令牌在这里就验不过
    key = _key_for("adm" if accepted == ("adm",) else None, secret)
    expected = _sign(f"{version}.{body}", key)
    # 定长比较，避免按字节短路造成的时序侧信道
    if not hmac.compare_digest(expected, signature):
        raise AuthError("签名不匹配")

    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("payload 解析失败") from exc
    if not isinstance(payload, dict):
        raise AuthError("payload 不是对象")

    # 宿主实现不会带 typ，缺省即身份令牌；但下载授权是显式 "dl"，
    # 必须拒绝，否则一条下载链接就能当登录态用。管理令牌 "adm" 同理只认管理接口。
    if payload.get("typ") not in accepted:
        raise AuthError("令牌类型不符")

    uid = str(payload.get("uid") or "").strip()
    if not uid:
        raise AuthError("payload 缺少 uid")

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise AuthError("payload 缺少 exp")
    if int(time.time()) > exp + _CLOCK_SKEW_SECONDS:
        raise AuthError("令牌已过期")

    return Principal(
        user_id=uid,
        name=str(payload.get("name") or "").strip(),
        expires_at=exp,
        auth_method="debug_token" if uid.startswith(DEBUG_UID_PREFIX) else "host_token",
    )


def extract_token(request: Request) -> str:
    """从请求里取令牌：优先 ``Authorization: Bearer``，兼容 ``X-Host-Token``。"""
    header = (request.headers.get("authorization") or "").strip()
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return (request.headers.get("x-host-token") or "").strip()


async def optional_principal(request: Request) -> Principal | None:
    """FastAPI 依赖：能验签就返回身份，不能就返回 ``None``（不抛 401）。

    给「还有另一条通行方式」的端点用，典型是 ``/api/files/download``——它也接受
    链接自带的下载授权。**必须写成依赖而不是在函数体里手调 require_principal()**，
    否则 FastAPI 的 dependency_overrides 不生效，测试没法替换身份。
    """
    settings = get_settings()
    if not settings.agent_auth_enabled:
        anon = (request.headers.get("x-host-user") or "").strip() or "dev"
        return Principal(user_id=anon, name="本地开发", auth_method="anonymous")
    try:
        return verify_token(extract_token(request))
    except AuthError:
        return None


async def require_principal(request: Request) -> Principal:
    """FastAPI 依赖：解析并验签令牌，失败一律 401。

    鉴权关闭时（``AGENT_AUTH_ENABLED=false``，仅限本地开发）退化为匿名身份，
    此时 ``session_id`` 仍由服务端派生，客户端依旧无法自选。
    """
    settings = get_settings()
    if not settings.agent_auth_enabled:
        anon = (request.headers.get("x-host-user") or "").strip() or "dev"
        return Principal(user_id=anon, name="本地开发", auth_method="anonymous")

    try:
        return verify_token(extract_token(request))
    except AuthError as exc:
        # 对外只给笼统原因，避免把「签名错」和「已过期」的差别透给攻击者；
        # 具体原因留在服务端日志里。
        raise HTTPException(
            status_code=401,
            detail="登录态无效或已过期，请在主产品中重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------- 下载授权
#
# 企微通道把下载链接发到聊天里给用户点，那次请求不可能带 Authorization 头，
# 所以下载链接自带一个**路径级**的签名授权（capability URL，同 S3 预签名的思路）：
# 授权只对签发时那一个 workspace_path 有效，且有独立时效。
#
# 代价是令牌出现在 URL 里，可能进代理日志/Referer。因此它 typ="dl"、不含身份、
# 换不到任何其他接口的访问权，且 TTL 独立可调。


def issue_download_grant(
    workspace_path: str,
    *,
    ttl_seconds: int | None = None,
    secret: str | None = None,
) -> str:
    """为单个工作区路径签发下载授权。"""
    rel = str(workspace_path or "").strip()
    if not rel:
        raise AuthError("workspace_path 不能为空")
    key = resolve_secret(secret)
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else get_settings().agent_download_grant_ttl_seconds
    )
    payload = {"typ": "dl", "p": rel, "exp": int(time.time()) + int(ttl)}
    body = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{_VERSION}.{body}"
    return f"{signing_input}.{_sign(signing_input, key)}"


def verify_download_grant(token: str, *, secret: str | None = None) -> str:
    """验签下载授权，返回被授权的 workspace_path。"""
    raw = (token or "").strip()
    if not raw:
        raise AuthError("下载授权为空")
    if len(raw.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise AuthError("下载授权过长")

    parts = raw.split(".")
    if len(parts) != 3:
        raise AuthError("下载授权格式错误")
    version, body, signature = parts
    if version != _VERSION:
        raise AuthError(f"下载授权版本不支持: {version}")

    key = resolve_secret(secret)
    if not hmac.compare_digest(_sign(f"{version}.{body}", key), signature):
        raise AuthError("下载授权签名不匹配")

    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("下载授权解析失败") from exc
    if not isinstance(payload, dict):
        raise AuthError("下载授权 payload 不是对象")
    # 身份令牌缺省无 typ，这里要求必须显式为 "dl"，堵住反向冒充
    if payload.get("typ") != "dl":
        raise AuthError("下载授权类型不符")

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise AuthError("下载授权缺少 exp")
    if int(time.time()) > exp + _CLOCK_SKEW_SECONDS:
        raise AuthError("下载授权已过期")

    rel = str(payload.get("p") or "").strip()
    if not rel:
        raise AuthError("下载授权缺少路径")
    return rel
