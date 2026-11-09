"""基于官方 wecom-aibot-python-sdk 的薄封装。"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class StructlogSdkLogger:
    """把 SDK 默认 print 日志接到 structlog，便于排查断线/踢线。"""

    def debug(self, message: str, *args: Any) -> None:
        logger.debug("wecom_sdk", msg=_fmt(message, args))

    def info(self, message: str, *args: Any) -> None:
        logger.info("wecom_sdk", msg=_fmt(message, args))

    def warn(self, message: str, *args: Any) -> None:
        logger.warning("wecom_sdk", msg=_fmt(message, args))

    def error(self, message: str, *args: Any) -> None:
        logger.error("wecom_sdk", msg=_fmt(message, args))


def _fmt(message: str, args: tuple[Any, ...]) -> str:
    if not args:
        return str(message)
    try:
        return str(message) % args
    except Exception:  # noqa: BLE001
        return f"{message} {args}"


def create_ws_client(*, bot_id: str, secret: str) -> Any:
    """创建官方 WSClient；未安装 SDK 时抛出明确错误。"""
    try:
        from aibot import WSClient, WSClientOptions
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "企微长连接需要安装 wecom-aibot-python-sdk："
            "pip install wecom-aibot-python-sdk"
        ) from exc

    return WSClient(
        WSClientOptions(
            bot_id=bot_id,
            secret=secret,
            max_reconnect_attempts=-1,
            logger=StructlogSdkLogger(),
        )
    )
