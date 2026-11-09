"""HTTP API 通道：按 channels.yaml 挂载/停用 FastAPI 路由。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from csagent.channels.base import ChannelAdapter

if TYPE_CHECKING:
    from csagent.channels.manager import ChannelContext


class HttpApiChannel(ChannelAdapter):
    """标准 HTTP/SSE 入口。

    路由实现仍在 api/router_chat.py；本通道负责挂载与 enabled 语义。
    """

    channel_name = "http_api"

    def __init__(
        self,
        *,
        context: ChannelContext | None = None,
        name: str = "http_api",
        config: dict[str, Any] | None = None,
    ) -> None:
        self.channel_name = name or "http_api"
        self._context = context
        self._config = dict(config or {})
        self._started = False
        self._router_mounted = False

    async def start(self, **kwargs: Any) -> None:
        app = self._context.app if self._context is not None else None
        if app is not None and not self._router_mounted:
            from csagent.api.router_admin import router as admin_router
            from csagent.api.router_auth import router as auth_router
            from csagent.api.router_branding import router as branding_router
            from csagent.api.router_chat import router as chat_router
            from csagent.api.router_files import router as files_router
            from csagent.api.router_reports import router as reports_router
            from csagent.api.router_skills import router as skills_router

            app.include_router(chat_router)
            app.include_router(files_router)
            app.include_router(reports_router)
            app.include_router(skills_router)
            # 端点内部按 AGENT_AUTH_DEV_LOGIN 自行 404，生产挂上也不会暴露
            app.include_router(auth_router)
            app.include_router(admin_router)
            app.include_router(branding_router)
            self._router_mounted = True
        self._started = True

    async def stop(self) -> None:
        # FastAPI 无法干净卸载 router；靠 is_running + 路由侧 503 闸门
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    @property
    def status(self) -> dict[str, Any]:
        return {
            "channel": self.channel_name,
            "status": "running" if self._started else "stopped",
            "router_mounted": self._router_mounted,
            "max_message_length": self._config.get("max_message_length"),
        }
