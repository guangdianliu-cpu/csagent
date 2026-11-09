"""Skill 管理 API（列表 / 详情 / 热重载）。

列表与详情给已登录的用户看（小窗的能力清单）；热重载会改变整个进程的 Skill 集合，
是运维动作，只认管理员令牌，并写安全审计。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from csagent.api.router_admin import require_admin
from csagent.audit.security_logger import record_security_event
from csagent.infra.auth import Principal, require_principal
from csagent.skills.discovery import SkillDiscovery

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _get_discovery(request: Request) -> SkillDiscovery:
    """从 app.state 获取 SkillDiscovery 实例。"""
    discovery = getattr(request.app.state, "skill_discovery", None)
    if discovery is None:
        raise HTTPException(status_code=503, detail="Skill 系统未初始化")
    return discovery


@router.get("")
async def list_skills(
    request: Request,
    _: Principal = Depends(require_principal),
):
    """列出所有已加载的 Skill。

    返回格式：
    {
      "count": N,
      "skills": [
        {"name": ..., "description": ..., "version": ..., "tags": [...],
         "enabled": bool, "source": ..., "has_allowed_tools": bool}
      ]
    }
    """
    discovery = _get_discovery(request)
    packages = discovery.scan()
    items = [
        {
            "name": pkg.name,
            "description": pkg.description,
            "version": pkg.version,
            "tags": pkg.tags,
            "enabled": pkg.enabled,
            "source": pkg.source,
            "has_allowed_tools": pkg.allowed_tools is not None,
        }
        for pkg in sorted(packages, key=lambda p: p.name)
    ]
    return {"count": len(items), "skills": items}


@router.get("/{name}")
async def get_skill(
    name: str,
    request: Request,
    _: Principal = Depends(require_principal),
):
    """返回指定 Skill 的详情。

    返回格式：
    {
      "name": ..., "description": ..., "version": ..., "tags": [...],
      "enabled": bool, "source": ...,
      "allowed_tools": [...] | null,
      "resources": [...],
      "body_chars": int,
      "root": str,
    }
    """
    discovery = _get_discovery(request)
    pkg = discovery.get(name)
    if pkg is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' 不存在")
    return {
        "name": pkg.name,
        "description": pkg.description,
        "version": pkg.version,
        "tags": pkg.tags,
        "enabled": pkg.enabled,
        "source": pkg.source,
        "allowed_tools": pkg.allowed_tools,
        "resources": pkg.resources,
        "body_chars": len(pkg.body),
        "root": str(pkg.root),
    }


@router.post("/reload")
async def reload_skills(
    request: Request,
    principal: Principal = Depends(require_admin),
):
    """触发 Skill 热重载（仅管理员）。

    返回重载后的 Skill 数量和名称列表。
    """
    discovery = _get_discovery(request)
    discovery.reload()
    packages = discovery.scan()
    names = [pkg.name for pkg in sorted(packages, key=lambda p: p.name)]
    record_security_event(
        "skills_reloaded", actor=principal.user_id, request=request, skills=names
    )
    return {
        "status": "reloaded",
        "count": len(names),
        "skills": names,
    }
