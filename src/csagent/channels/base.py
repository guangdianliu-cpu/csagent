"""入口通道适配器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ChannelAdapter(ABC):
    """所有入口通道的统一接口。

    职责：
    1. 接收原始通道消息，转换为 UnifiedMessage
    2. 将 UnifiedReply 格式化为通道协议
    3. 管理通道连接生命周期（start / stop）
    """

    channel_name: str = "unknown"

    @abstractmethod
    async def start(self, **kwargs: Any) -> None:
        """启动通道（建立连接、注册路由等）。"""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """关闭通道连接。"""
        raise NotImplementedError

    @property
    def status(self) -> dict[str, Any]:
        """通道运行状态（供 /health 接口使用）。"""
        return {"channel": self.channel_name, "status": "unknown"}
