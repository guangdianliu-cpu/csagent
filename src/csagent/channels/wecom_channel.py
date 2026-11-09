"""企微长连接通道：包装 WeComAgentBridge，统一经 MessageGateway。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from csagent.channels.base import ChannelAdapter
from csagent.channels.message_gateway import MessageGateway
from csagent.infra.config import Settings
from csagent.agent import Agent as MainAgent
from csagent.wecom.bridge import WeComAgentBridge

if TYPE_CHECKING:
    from csagent.channels.manager import ChannelContext

logger = structlog.get_logger(__name__)


class WeComChannel(ChannelAdapter):
    """企微智能机器人通道。"""

    channel_name = "wecom"

    def __init__(
        self,
        *,
        context: ChannelContext | None = None,
        name: str = "wecom",
        config: dict[str, Any] | None = None,
        main_agent: MainAgent | None = None,
        settings: Settings | None = None,
        message_gateway: MessageGateway | None = None,
    ) -> None:
        self.channel_name = name or "wecom"
        self._config = dict(config or {})
        if context is not None:
            self._main_agent = context.main_agent
            self._settings = context.settings
            self._message_gateway = context.message_gateway
        else:
            if main_agent is None or settings is None or message_gateway is None:
                raise TypeError("WeComChannel 需要 context 或 main_agent/settings/message_gateway")
            self._main_agent = main_agent
            self._settings = settings
            self._message_gateway = message_gateway
        self._bridge: WeComAgentBridge | None = None

    async def start(self, **kwargs: Any) -> None:
        if not self._settings.wecom_enabled:
            logger.info("wecom_channel_skipped_disabled")
            return
        bridge = WeComAgentBridge(
            main_agent=self._main_agent,
            settings=self._settings,
            message_gateway=self._message_gateway,
        )
        await bridge.start()
        self._bridge = bridge
        logger.info("wecom_channel_started", config_keys=list(self._config.keys()))

    async def stop(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None

    @property
    def bridge(self) -> WeComAgentBridge | None:
        return self._bridge

    @property
    def status(self) -> dict[str, Any]:
        if self._bridge is None:
            return {"channel": self.channel_name, "status": "stopped"}
        return {
            "channel": self.channel_name,
            "status": "running",
            "wecom": self._bridge.status,
        }
