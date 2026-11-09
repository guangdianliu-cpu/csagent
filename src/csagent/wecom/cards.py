"""企微互动卡片：高危操作的批准 / 拒绝。

卡片和点击靠 ``task_id`` 对上；按钮键见下面三个常量。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 按钮键：点击回调里的 event_key
CONFIRM_ACTION = "confirm_action"
CANCEL_ACTION = "cancel_action"
# 处理过的卡片只剩一个置灰按钮，点它的回调直接忽略
DISABLED_ACTION = "disabled"

# 卡片上的时刻一律按北京时间显示：服务器 / 容器常是 UTC，按本机时区会差 8 小时。
# 用固定偏移而不是 ZoneInfo("Asia/Shanghai")：Windows 上没装 tzdata 时后者会报错
BEIJING_TZ = timezone(timedelta(hours=8), "北京时间")


def build_confirmation_card(
    *,
    task_id: str,
    summary: str,
    tool_name: str,
    reason: str,
    from_user: str,
    chat_type: str,
    queued: int = 0,
    expires_at: float | None = None,
) -> dict[str, Any]:
    """待审批卡片。文案来自策略挂起时的说明，不写死文件路径。

    ``queued``：同一任务里排在它后面的确认数。企微卡片发出后改不了，后面的要等
    这张处理完才发，这里先告诉用户还有几项。
    ``expires_at``：确认簿里这张卡片的过期时刻（时间戳），显示成「HH:MM 前有效」；
    不写死时长，有效期改了、排队后重新计时，卡片上都跟着对。
    """
    scope_text = "当前群聊（全员可见）" if chat_type == "group" else "当前对话"
    quote = (summary or tool_name or "待确认操作").strip()
    if len(quote) > 120:
        quote = quote[:119] + "…"
    desc = reason.strip() if reason else "命中策略，需要您确认后才会执行"
    return {
        "card_type": "button_interaction",
        "source": {"desc": "待您审批", "desc_color": 0},
        "main_title": {"title": "需要您确认这项操作", "desc": desc},
        "quote_area": {"type": 0, "title": "拟执行的操作", "quote_text": quote},
        "horizontal_content_list": [
            {"keyname": "执行工具", "value": tool_name or "-"},
            {"keyname": "发布范围", "value": scope_text},
            {"keyname": "发起人", "value": from_user or "-"},
            *(
                [{"keyname": "有效时间", "value": f"{_clock(expires_at)} 前有效"}]
                if expires_at else []
            ),
            *(
                [{"keyname": "后续确认", "value": f"还有 {queued} 项，处理完这张后发出"}]
                if queued > 0 else []
            ),
        ],
        "button_list": [
            {"text": "批准执行", "style": 1, "key": CONFIRM_ACTION},
            {"text": "拒绝驳回", "style": 2, "key": CANCEL_ACTION},
        ],
        "task_id": task_id,
    }


def _clock(timestamp: float) -> str:
    """时间戳 → 北京时间的 ``HH:MM``。"""
    return datetime.fromtimestamp(timestamp, BEIJING_TZ).strftime("%H:%M")


def build_updated_card(
    *,
    task_id: str,
    approved: bool,
    operator: str,
    summary: str,
) -> dict[str, Any]:
    """点击后就地替换成已处理。企微要求在几秒内更新，所以先改卡片再执行工具。"""
    confirmed = approved
    quote = (summary or "已处理的操作").strip()
    if len(quote) > 120:
        quote = quote[:119] + "…"
    return {
        "card_type": "button_interaction",
        "source": {"desc": "您已审批", "desc_color": 1},
        "main_title": {
            "title": "操作已批准" if confirmed else "操作已驳回",
            "desc": "正在执行，完成后会另发一条结果。" if confirmed else "不会执行这项操作。",
        },
        "quote_area": {"type": 0, "title": "关联操作", "quote_text": quote},
        "horizontal_content_list": [
            {
                "keyname": "审批状态",
                "value": f"已由 @{operator} 批准" if confirmed else f"已由 @{operator} 驳回",
            },
            {"keyname": "处理时间", "value": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")},
        ],
        "button_list": [
            {
                "text": "已批准" if confirmed else "已驳回",
                "style": 2,
                "key": DISABLED_ACTION,
            }
        ],
        "task_id": task_id,
    }


def parse_template_card_event(frame: dict[str, Any]) -> dict[str, str]:
    """从企微回调帧里取出 task_id、按钮键和点击人。"""
    body = frame.get("body") or {}
    if not isinstance(body, dict):
        body = {}
    event = body.get("event") or {}
    if not isinstance(event, dict):
        event = {}
    detail = event.get("template_card_event") or {}
    if not isinstance(detail, dict):
        detail = {}
    operator = str((body.get("from") or {}).get("userid") or "")
    return {
        "task_id": str(detail.get("task_id") or event.get("task_id") or ""),
        "event_key": str(detail.get("event_key") or event.get("event_key") or ""),
        "operator": operator,
        "chatid": str(body.get("chatid") or operator),
        "chattype": str(body.get("chattype") or "single").lower(),
    }
