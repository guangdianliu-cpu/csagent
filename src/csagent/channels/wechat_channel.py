"""微信群通道：经发送机（wecom-sender）读群里 @我 的消息，统一走 MessageGateway，再经发送机回复。

本服务是服务端：在这里把发送机要访问的 /api/wechat/link/* 挂到应用上，发送机凭 WECHAT_LINK_TOKEN 连进来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from csagent.channels.base import ChannelAdapter
from csagent.wechat.bridge import WeChatAtBridge, WeChatConfig

if TYPE_CHECKING:
    from csagent.channels.manager import ChannelContext

logger = structlog.get_logger(__name__)


class WeChatChannel(ChannelAdapter):
    channel_name = "wechat"

    def __init__(
        self,
        *,
        context: ChannelContext,
        name: str = "wechat",
        config: dict[str, Any] | None = None,
    ) -> None:
        self.channel_name = name or "wechat"
        self._context = context
        self._config = WeChatConfig.from_dict(config)
        self._bridge: WeChatAtBridge | None = None
        self._router_mounted = False

    async def start(self, **kwargs: Any) -> None:
        bridge = WeChatAtBridge(
            settings=self._context.settings,
            message_gateway=self._context.message_gateway,
            config=self._config,
        )
        if not self._router_mounted:
            if self._context.app is None:
                raise RuntimeError("发送机要连本服务的 HTTP 接口，但通道没有拿到应用实例")
            self._context.app.include_router(bridge.link.router())
            self._router_mounted = True
        await bridge.start()
        self._bridge = bridge
        logger.info("wechat_channel_started", reply_mode=self._config.reply_mode)

    async def stop(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None

    @property
    def status(self) -> dict[str, Any]:
        if self._bridge is None:
            return {"channel": self.channel_name, "status": "stopped"}
        return {"channel": self.channel_name, "status": "running", "wechat": self._bridge.status}
