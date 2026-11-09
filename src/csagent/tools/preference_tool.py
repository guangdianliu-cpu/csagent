"""回复风格偏好：独立于长期客户画像的小工具 + 文件存储。

只存"回复风格"这一件事，注入时作为强指令而非参考资料；
按会话归属落盘（私聊=用户，群聊=群），web 端为 web:{userId}。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool

logger = structlog.get_logger(__name__)

_VALID_STYLES = frozenset({"concise", "detailed", "custom", "normal"})
# 2000 字：够装一整套输出模板。实测一份十来个字段的卡片式模板约 430 字，
# 旧的 300 字上限连模板本身都放不下，更别说附带的口径规则。
# 别再往上放：note 每轮都注入进上下文，涨的是每一轮的 token。
_MAX_NOTE_CHARS = 2000

_STYLE_DIRECTIVES = {
    "concise": "回复保持简短，直接给结论和关键数字，省略背景铺垫，除非用户本轮明确要求展开。",
    "detailed": "回复尽量详细完整，主动补充相关背景、口径说明，不要只给结论。",
    "normal": "回复风格恢复默认：按内容需要给结论和必要说明，不特意精简或展开。",
}


def preference_key(scope: CustomerScope) -> str:
    """派生偏好归属 key：私聊按用户、群聊按群共享。

    会话本身已是这个粒度（企微私聊 `wecom:{from_user}`、群聊 `wecom:group:{chatid}`，
    见 wecom/bridge.py），直接用 session_id。
    """
    return scope.session_id or ""


class ReplyPreferenceStore:
    """按 key 落盘的回复风格偏好（单条 JSON 文件，无版本/过期概念）。"""

    def __init__(self, root_dir: Path | str) -> None:
        self._dir = Path(root_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, style: str, note: str = "") -> None:
        path = self._path(key)
        path.write_text(
            json.dumps(
                {
                    "style": style,
                    "note": note,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def delete(self, key: str) -> None:
        if not key:
            return
        path = self._path(key)
        if path.exists():
            path.unlink()

    def format_for_context(self, key: str) -> str:
        """指令式措辞，供拼进 context_summary 最前面。"""
        data = self.get(key)
        if not data:
            return ""
        style = data.get("style")
        note = str(data.get("note") or "").strip()
        if style == "custom":
            if not note:
                return ""
            return f"【用户回复风格偏好，必须遵守】{note}"
        directive = _STYLE_DIRECTIVES.get(style)
        if not directive:
            return ""
        text = f"【用户回复风格偏好，必须遵守】{directive}"
        if note:
            text += f"（用户备注：{note}）"
        return text

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^\w.\-]", "_", key)[:120] or "unknown"
        return self._dir / f"{safe}.json"


class SetReplyStyleTool(BaseTool):
    """LLM 检测到用户表达回复风格诉求时调用，写入 ReplyPreferenceStore。"""

    name = "set_reply_style"
    description = (
        "记住用户对回复风格/格式的偏好（简短/详细/自定义要求/恢复默认）。"
        "仅当用户明确表达此类诉求时调用，例如「以后回复简短点」「说详细点」"
        "「开头结尾都加123」「用英文回复」。"
        "style=custom 时必须在 note 里完整写清楚用户的具体要求（该要求会原样作为指令使用）。"
        "若用户给的是一整套输出模板（固定版式、字段顺序、占位符），"
        "把模板**逐字**写进 note，连换行、标点、占位符名字都不要改动——"
        "模板的价值就在于每次输出完全一致，复述时擅自润色等于毁掉它。"
        "不要因为用户在问产品问题就调用。"
        "【只存当前一份，不留历史】新偏好直接覆盖旧的，normal 直接清除，"
        "均不可撤销、无备份、无版本记录。用户要求「回滚/恢复上一版偏好」时，"
        "本工具做不到——直接告知并请其重新提供内容，不要试图从别处找回旧值。"
    )

    def __init__(self, store: ReplyPreferenceStore) -> None:
        self._store = store

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "style": {
                    "type": "string",
                    "enum": sorted(_VALID_STYLES),
                    "description": (
                        "concise=简短，detailed=详细，"
                        "custom=简短/详细之外的其他格式要求（需配合 note），"
                        "normal=恢复默认风格"
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "style=custom 时必填：完整、精确描述用户的格式要求"
                        "（如「回复开头和结尾都加123」）。"
                        "承载整套输出模板时逐字照抄，不要压缩或改写。"
                        "其他 style 下可选，作为补充说明。"
                    ),
                },
            },
            "required": ["style"],
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        style = str(arguments.get("style") or "").strip()
        if style not in _VALID_STYLES:
            return f"参数错误：style 必须是 {', '.join(sorted(_VALID_STYLES))} 之一。"
        note = str(arguments.get("note") or "").strip()[:_MAX_NOTE_CHARS]
        if style == "custom" and not note:
            return "参数错误：style=custom 时必须在 note 里写清楚具体格式要求。"
        key = preference_key(scope)
        if not key:
            return "当前会话缺少可用于记忆偏好的身份信息，本次未保存。"
        if style == "normal":
            self._store.delete(key)
            logger.info("reply_style_cleared", key=key)
            return "已清除该回复风格偏好，之后按默认方式回复。旧偏好不保留副本，无法回滚。"
        self._store.set(key, style, note)
        logger.info("reply_style_set", key=key, style=style)
        return "已记住该回复风格偏好，之后会按此调整。已覆盖此前的偏好，旧值不保留。"
