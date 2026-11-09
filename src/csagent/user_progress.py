"""面向用户的进度文案 + 工具进度映射。"""

from __future__ import annotations

SESSION_START = "正在理解您的问题..."

# 并发闸门满员时先回这一句再排队（M2.2）。不静默等待是关键：用户看不到任何反馈会
# 以为消息没发出去，于是重发——那正好又多占一个名额，把拥塞推得更高。
# 不报「前面还有几个」，是因为 asyncio.Semaphore 没有可靠的公开等待计数，
# 编一个数字比不报更糟。
QUEUED_NOTICE = "当前处理中的请求较多，您的问题已排队，请稍候..."

_TOOL_PROGRESS: dict[str, str] = {
    "web_search": "正在搜索...",
    "http_call": "正在请求接口...",
    "run_script": "正在执行脚本...",
    "run_shell": "正在执行命令...",
    "write_file": "正在写入文件...",
    "read_file": "正在读取文件...",
    "excel_tool": "正在处理表格...",
    "send_file": "正在准备发送文件...",
}


def progress_for_tool(tool_name: str, *args, **kwargs) -> str | None:
    """按工具名返回进度文案；未知工具返回 None。"""
    return _TOOL_PROGRESS.get(tool_name)


# 企微流式气泡按纯文本展示，不渲染 Markdown。清单用符号打勾，
# 完成一项改这一条，而不是再推一行「pending → completed」。
TODO_CHECKLIST_TITLE = "待办"

_TODO_MARKS = {
    "completed": "✅",
    "in_progress": "⏳",
    "pending": "☐",
    "cancelled": "🚫",
}
_CLOSED_STATUSES = frozenset({"completed", "cancelled"})


def format_todo_checklist(items: list[dict]) -> str:
    """把当前待办渲染成勾选清单。

    :param items: 按计划顺序排列，每项含 content、status
    :returns: 第一行是「待办」，后面一行一项；没有内容的项跳过
    """
    rows: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split())
        if not content:
            continue
        rows.append((str(item.get("status") or "pending"), content))

    # 全部做完：只剩标题。渠道见到它就把清单从气泡里拿掉，接着出答案
    if rows and all(status in _CLOSED_STATUSES for status, _ in rows):
        return TODO_CHECKLIST_TITLE

    lines = [TODO_CHECKLIST_TITLE]
    for status, content in rows:
        lines.append(f"{_TODO_MARKS.get(status, '☐')} {content}")
    return "\n".join(lines)


def is_todo_all_done(text: str) -> bool:
    """清单是不是「全部做完」：只有标题、没有任何一项。"""
    return (text or "").strip() == TODO_CHECKLIST_TITLE


def is_todo_checklist(text: str) -> bool:
    """进度文案是不是待办清单。渠道用它把清单钉住，避免被下一句工具进度盖掉。"""
    head, _, _rest = (text or "").partition("\n")
    return head.strip() == TODO_CHECKLIST_TITLE
