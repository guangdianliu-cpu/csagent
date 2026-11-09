"""通道装配上下文与声明式 ChannelManager。"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import FastAPI

from csagent.channels.base import ChannelAdapter
from csagent.channels.message_gateway import MessageGateway
from csagent.infra.config import CONFIGS_DIR, Settings
from csagent.agent import Agent as MainAgent

logger = structlog.get_logger(__name__)


@dataclass
class ChannelContext:
    """注入所有通道适配器的共享依赖。"""

    settings: Settings
    main_agent: MainAgent
    message_gateway: MessageGateway
    app: FastAPI | None = None


def load_channels_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or (CONFIGS_DIR / "channels.yaml")
    if not cfg_path.exists():
        logger.warning("channels_yaml_not_found", path=str(cfg_path))
        return {}
    with cfg_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    channels = raw.get("channels") or {}
    return channels if isinstance(channels, dict) else {}


def _import_adapter(dotted_path: str) -> type:
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class ChannelManager:
    """按 channels.yaml 的 adapter / enabled / config 装配通道。"""

    def __init__(
        self,
        *,
        settings: Settings,
        main_agent: MainAgent,
        message_gateway: MessageGateway,
        channels_config: dict[str, Any] | None = None,
        app: FastAPI | None = None,
    ) -> None:
        self._context = ChannelContext(
            settings=settings,
            main_agent=main_agent,
            message_gateway=message_gateway,
            app=app,
        )
        self._config = channels_config if channels_config is not None else load_channels_config()
        self._channels: dict[str, ChannelAdapter] = {}

    @property
    def channels(self) -> dict[str, ChannelAdapter]:
        return dict(self._channels)

    def get(self, name: str) -> ChannelAdapter | None:
        return self._channels.get(name)

    async def start_all(self) -> None:
        for name, cfg in self._config.items():
            if not isinstance(cfg, dict):
                continue
            if not self._is_enabled(cfg, default=False):
                logger.info("channel_disabled", channel=name)
                continue
            if not self._should_start(name, cfg):
                continue
            try:
                channel = self._instantiate(name, cfg)
                await channel.start()
                self._channels[name] = channel
                logger.info(
                    "channel_started",
                    channel=name,
                    adapter=str(cfg.get("adapter") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("channel_start_failed", channel=name, error=str(exc))

    async def stop_all(self) -> None:
        for name, channel in list(self._channels.items()):
            try:
                await channel.stop()
                logger.info("channel_stopped", channel=name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("channel_stop_failed", channel=name, error=str(exc))
        self._channels.clear()

    def status(self) -> dict[str, Any]:
        return {name: ch.status for name, ch in self._channels.items()}

    def _should_start(self, name: str, cfg: dict[str, Any]) -> bool:
        # 企微还需环境凭证；yaml enabled 不够
        if name == "wecom" and not self._context.settings.wecom_enabled:
            logger.info(
                "channel_skipped",
                channel="wecom",
                reason="channels.yaml enabled but WECOM_ENABLED=false",
            )
            return False
        # 微信群通道：发送机靠 WECHAT_LINK_TOKEN 连上来
        if name == "wechat" and not self._context.settings.wechat_link_token.strip():
            logger.info(
                "channel_skipped",
                channel="wechat",
                reason="channels.yaml enabled but WECHAT_LINK_TOKEN is empty",
            )
            return False
        adapter = str(cfg.get("adapter") or "").strip()
        if not adapter:
            logger.warning("channel_missing_adapter", channel=name)
            return False
        return True

    def _instantiate(self, name: str, cfg: dict[str, Any]) -> ChannelAdapter:
        adapter_path = str(cfg.get("adapter") or "").strip()
        adapter_cls = _import_adapter(adapter_path)
        config = dict(cfg.get("config") or {})
        # 优先新签名；兼容旧构造
        try:
            return adapter_cls(
                context=self._context,
                name=name,
                config=config,
            )
        except TypeError:
            if name == "wecom":
                return adapter_cls(
                    main_agent=self._context.main_agent,
                    settings=self._context.settings,
                    message_gateway=self._context.message_gateway,
                )
            return adapter_cls()

    @staticmethod
    def _is_enabled(cfg: Any, *, default: bool) -> bool:
        if not isinstance(cfg, dict):
            return default
        if "enabled" not in cfg:
            return default
        return bool(cfg.get("enabled"))
