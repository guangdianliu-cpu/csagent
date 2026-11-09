"""联调用的令牌签发端点。

**生产必须关闭**（``AGENT_AUTH_DEV_LOGIN=false``，默认即 false）。开启后任何人
都能为任意 uid 换到令牌，等于没有鉴权——它存在的唯一目的，是让本地开发和联调
阶段能跑在与生产**完全相同**的鉴权代码路径上，而不是靠一个 mock 分支绕过去。

生产环境的令牌由宿主服务端用共享密钥自行签发，不经过本服务，
算法见 docs/host-integration.md。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from csagent.audit.security_logger import record_security_event
from csagent.infra.auth import AuthError, issue_token
from csagent.infra.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class DevTokenRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(default="", max_length=64)


class DevTokenResponse(BaseModel):
    token: str
    expires_in: int


@router.post("/dev-token", response_model=DevTokenResponse)
async def dev_token(body: DevTokenRequest, request: Request) -> DevTokenResponse:
    settings = get_settings()
    if not settings.agent_auth_dev_login:
        # 404 而非 403：不确认这个端点在本部署里存在过
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        token = issue_token(body.user_id, name=body.name)
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=f"签发失败: {exc}") from exc
    # 免鉴权换令牌等于冒充任意用户：哪怕只在联调环境开着，也要留下谁换了谁
    record_security_event("dev_token_issued", request=request, target_uid=body.user_id)
    return DevTokenResponse(token=token, expires_in=settings.agent_token_ttl_seconds)
