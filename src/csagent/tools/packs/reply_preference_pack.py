"""回复风格偏好工具包：记住用户对回复格式的偏好，按会话归属存储。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.preference_tool import ReplyPreferenceStore, SetReplyStyleTool

logger = structlog.get_logger(__name__)


class ReplyPreferenceToolPack(ToolPack):
    """回复风格偏好（set_reply_style）。"""

    pack_name = "reply_preference"
    description = "记住用户回复风格偏好（简短/详细/自定义），按会话归属存储"

    def __init__(self) -> None:
        self._store: ReplyPreferenceStore | None = None
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        settings = get_settings()
        store_dir = settings.reply_preference_dir or str(PROJECT_ROOT / "data" / "reply_preference")
        if not Path(store_dir).is_absolute():
            store_dir = str(PROJECT_ROOT / store_dir)

        self._store = ReplyPreferenceStore(store_dir)
        tool = SetReplyStyleTool(self._store)

        for item in config.get("tools") or []:
            if (
                isinstance(item, dict)
                and item.get("name") == tool.name
                and item.get("description")
            ):
                tool.description = str(item["description"])

        self._tools = [tool]
        logger.info("reply_preference_pack_ready", store_dir=store_dir, tools=[tool.name])

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)

    def get_store(self) -> ReplyPreferenceStore | None:
        """供 main.py 取出后注入 Agent（context 注入需要，见 reply_preference_store 参数）。"""
        return self._store
