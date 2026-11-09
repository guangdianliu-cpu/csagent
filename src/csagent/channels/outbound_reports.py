"""会话级出站报告队列：publish_report / update_report 入队，通道在本轮文字结束后推卡片。

与 ``outbound_files`` 同一套交接：网页端在 SSE ``done`` 之前取走；对话记录只 peek。
企微 / 微信群渲染不了面板，降级在 M10 做；在那之前它们不取，由 agent 每轮开始时
``clear_outbound_reports`` 清掉上一轮的残留，每个会话也只留最近几条。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock

import structlog

logger = structlog.get_logger(__name__)

MAX_PER_SESSION = 5


@dataclass(frozen=True)
class OutboundReport:
    report_id: str
    version: int
    title: str
    summary: str
    # 有表格才出「Excel」按钮
    has_tables: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


_lock = Lock()
_queues: dict[str, list[OutboundReport]] = {}


def _key(session_id: str) -> str:
    return (session_id or "").strip() or "unknown"


def enqueue_outbound_report(session_id: str, item: OutboundReport) -> None:
    """入队。同一份报告本轮改了几次，只留最后一版。"""
    key = _key(session_id)
    with _lock:
        bucket = [x for x in _queues.get(key, []) if x.report_id != item.report_id]
        bucket.append(item)
        _queues[key] = bucket[-MAX_PER_SESSION:]
    logger.info("outbound_report_enqueued", session_id=key, report_id=item.report_id, version=item.version)


def drain_outbound_reports(session_id: str) -> list[OutboundReport]:
    with _lock:
        return _queues.pop(_key(session_id), [])


def peek_outbound_reports(session_id: str) -> list[OutboundReport]:
    with _lock:
        return list(_queues.get(_key(session_id), []))


def clear_outbound_reports(session_id: str | None = None) -> None:
    """清空指定会话（每轮开始时）或全部（测试）。"""
    with _lock:
        if session_id is None:
            _queues.clear()
        else:
            _queues.pop(_key(session_id), None)
