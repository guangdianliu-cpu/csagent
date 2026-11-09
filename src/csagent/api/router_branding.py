"""公开的品牌外观读取接口（免鉴权）。

给宿主页面里的 SDK 用：它运行在**宿主的域名**下，跨域读取，所以
``/api/branding`` 显式放开 CORS。返回内容只有图标地址与显示方式，不含任何
用户数据，放开是安全的。

图标本身用 ``<img>`` 加载，不需要 CORS；但 SVG 可能内嵌脚本，直接在浏览器
地址栏打开时会在本域执行，所以图标响应一律加 CSP 沙箱。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from csagent.admin import branding

router = APIRouter(prefix="/api/branding", tags=["branding"])

_ICON_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    # 地址里带版本号，换图后地址就变了，可以放心长缓存
    "Cache-Control": "public, max-age=86400",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


@router.get("")
async def get_branding() -> JSONResponse:
    """SDK 启动时调用。icon_url 为相对本服务挂载路径的地址；未配置时为 null。"""
    state = branding.load_state()
    icon_url = (
        f"api/branding/icon?v={state.version}" if branding.icon_path(state) else None
    )
    return JSONResponse(
        {"icon_url": icon_url, "icon_mode": state.icon_mode, "version": state.version},
        headers={
            "Access-Control-Allow-Origin": "*",
            # 换图后宿主页刷新就能拿到新配置，不必等缓存过期
            "Cache-Control": "no-cache",
        },
    )


@router.get("/icon")
async def get_branding_icon() -> FileResponse:
    state = branding.load_state()
    path = branding.icon_path(state)
    if path is None:
        raise HTTPException(status_code=404, detail="未配置图标")
    media_type = branding.ICON_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers=_ICON_HEADERS)
