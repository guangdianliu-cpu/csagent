"""TODO 工具包：会话级待办事项管理。

M3.2：待办列表持久化到 ``data/todos/{session_id}.json``。
M3.5：未完成项由 ``format_open_todos_for_context`` 注入每轮上下文动态尾部。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from csagent.react.models import CustomerScope
from csagent.session_state import session_state_var
from csagent.tools.base import BaseTool, ToolPack
from csagent.user_progress import format_todo_checklist

logger = structlog.get_logger(__name__)

# 会话级 TODO 的进程内缓存（配合 TodoStore 落盘）；key 为 session_id
_global_todo_store: dict[str, dict[str, dict]] = {}

_VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

DEFAULT_PERSIST_DIR = "data/todos"
_OPEN_STATUSES = frozenset({"pending", "in_progress"})
# Pack 初始化时写入，供上下文注入从同一目录读盘。
_persist_dir: str = DEFAULT_PERSIST_DIR


class TodoStore:
    """待办列表的文件持久化存储。

    每个会话对应一个 JSON 文件（`{session_id}.json`），内容为
    ``{todo_id: {"id", "content", "status"}}`` 的字典。
    """

    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        # session_id 可能含路径分隔符等非法字符，做一次保守清洗防止路径穿越
        safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
        return self._base / f"{safe_id}.json"

    def save(self, session_id: str, todos: dict[str, dict]) -> None:
        path = self._path(session_id)
        try:
            path.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("todo_store_save_failed", session_id=session_id, exc_info=True)

    def load(self, session_id: str) -> dict[str, dict]:
        path = self._path(session_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("todo_store_load_failed", session_id=session_id, exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}


class TodoWriteTool(BaseTool):
    """创建/更新待办事项列表。"""

    name = "todo_write"
    description = (
        "创建或更新会话待办。每次调用都会立刻推一条清单到用户聊天里。"
        "预计 3 步以上的任务，在任何业务工具之前先单独调用一次："
        "正在做的那项 status=in_progress，其余 pending。"
        "每完成一步立刻再调用一次（merge=true），只把刚完成的那项改成 completed，"
        "下一项改成 in_progress。不要等全部做完再一次性标记，"
        "也不要和其他工具同一轮一起调用。"
        "最终回复里不要再贴待办清单。"
    )

    def __init__(self, store: TodoStore | None = None) -> None:
        self._store = store

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "待办 ID"},
                            "content": {"type": "string", "description": "待办内容"},
                            "status": {
                                "type": "string",
                                "enum": list(_VALID_STATUSES),
                                "description": "状态",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                    "description": "待办事项列表",
                },
                "merge": {
                    "type": "boolean",
                    "description": "true=合并到现有列表，false=替换（默认 false）",
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        todos_raw = arguments.get("todos")
        if not todos_raw or not isinstance(todos_raw, list):
            return "错误: 缺少 todos 参数或格式错误"

        merge = bool(arguments.get("merge", False))
        session_id = scope.session_id

        store = _global_todo_store.setdefault(session_id, {})

        if not merge:
            store.clear()

        errors: list[str] = []
        changes: list[str] = []

        for item in todos_raw:
            if not isinstance(item, dict):
                errors.append("待办项必须是对象")
                continue

            todo_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending").strip()

            if not todo_id:
                errors.append("待办项缺少 id")
                continue
            if not content:
                errors.append(f"待办 '{todo_id}' 缺少 content")
                continue
            if status not in _VALID_STATUSES:
                errors.append(f"待办 '{todo_id}' 状态无效: {status}")
                continue

            old = store.get(todo_id)
            store[todo_id] = {"id": todo_id, "content": content, "status": status}

            if old and old.get("status") != status:
                changes.append(f"[{todo_id}] {old.get('status')} → {status}")
            elif not old:
                changes.append(f"[{todo_id}] 新增: {content[:50]}")

        # M3.2：每次写入后立即落盘，保证跨会话/重启后 todo_read 可恢复
        if self._store is not None:
            self._store.save(session_id, store)

        # 推整份清单，不推「id pending → completed」这种日志。
        # 渠道把这份清单钉在进度气泡上，完成一项就改勾。
        if changes:
            await self._push_progress(store)

        # 构建结果
        total = len(store)
        stats: dict[str, int] = {}
        for t in store.values():
            s = t["status"]
            stats[s] = stats.get(s, 0) + 1

        parts = [f"共 {total} 个待办事项"]
        for s in ["pending", "in_progress", "completed", "cancelled"]:
            if s in stats:
                parts.append(f"{s}: {stats[s]}")

        result = "  ".join(parts)
        if errors:
            result += "\n\n⚠️ 警告:\n" + "\n".join(f"- {e}" for e in errors)

        return result

    @staticmethod
    async def _push_progress(store: dict[str, dict]) -> None:
        """把当前整份待办推到进度通道，供企微气泡原地打勾。

        :param store: 会话待办，键为 id，值含 content 与 status
        """
        state = session_state_var.get()
        if state is None or state.on_progress is None:
            return
        summary = format_todo_checklist(list(store.values()))
        try:
            await state.on_progress(summary)
        except Exception:  # noqa: BLE001
            logger.warning("todo_progress_push_failed", exc_info=True)


class TodoReadTool(BaseTool):
    """查看当前会话的待办事项列表（M3.2：支持跨进程重启后从磁盘恢复）。"""

    name = "todo_read"
    description = "查看当前会话的待办事项列表，恢复之前记录的待办状态"

    def __init__(self, store: TodoStore | None = None) -> None:
        self._store = store

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        session_id = scope.session_id
        store = _global_todo_store.get(session_id)

        # 进程内缓存为空时（如服务重启后首次访问），尝试从磁盘恢复
        if not store and self._store is not None:
            loaded = self._store.load(session_id)
            if loaded:
                store = _global_todo_store.setdefault(session_id, {})
                store.update(loaded)

        if not store:
            return "当前没有待办事项"

        lines = ["当前待办事项："]
        for todo_id in sorted(store):
            item = store[todo_id]
            lines.append(f"- [{item.get('status')}] {item.get('id')}: {item.get('content')}")
        return "\n".join(lines)


class TodoToolPack(ToolPack):
    """TODO 工具包。"""

    pack_name = "todo"
    description = "会话级 TODO 管理"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        persist_dir = (config or {}).get("persist_dir", DEFAULT_PERSIST_DIR)
        global _persist_dir
        _persist_dir = persist_dir
        store = TodoStore(persist_dir)
        self._tools = [TodoWriteTool(store), TodoReadTool(store)]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)


def format_open_todos_for_context(session_id: str) -> str:
    """当前会话未完成的待办，供 ContextPipeline 注入动态尾部（M3.5）。

    长任务的「我做到哪了」由这份确定性列表回答，不靠摘要回忆。
    已完成 / 已取消的不注入，避免把过期计划继续塞给模型。

    :param session_id: 会话标识
    :returns: Markdown 片段；没有未完成项时为空串
    """
    if not session_id:
        return ""
    store = _global_todo_store.get(session_id)
    if not store:
        loaded = TodoStore(_persist_dir).load(session_id)
        if loaded:
            store = _global_todo_store.setdefault(session_id, {})
            store.update(loaded)
    if not store:
        return ""
    open_items = [
        item
        for item in store.values()
        if isinstance(item, dict) and item.get("status") in _OPEN_STATUSES
    ]
    if not open_items:
        return ""
    lines = ["## 当前未完成待办"]
    for item in open_items:
        lines.append(
            f"- [{item.get('status')}] {item.get('id')}: {item.get('content')}"
        )
    return "\n".join(lines)

