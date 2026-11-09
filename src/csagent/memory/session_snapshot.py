"""会话级工具调用流水（跨请求保留「上一轮到底查了什么」）。

多轮对话里模型需要知道自己此前做过哪些工具调用，才能在第 N 轮回答
「第二条那个现在怎么样」这类指代问题。ShortTermMemory 只存 user/assistant
文本，工具调用不在里面；本模块按 ``session_id`` 保留最近若干次调用的轻量快照，
两个下游消费它：

- ``context_selector.select_tool_history`` —— 查询感知的 Top-N 精选，注入上下文
- ``format_tool_digest`` —— 完整流水，喂给 Checkpoint 压缩器

NOTE(历史): 本模块原本还存放「领域结构化快照」（每个 domain 最近一轮的
``structured_data``）。该层的写入方法 ``update()`` 从未被生产代码调用过，
``load()`` 恒返回空字典，却占着 Tier 2 三成的上下文预算，并通过
``context_pipeline`` 里一行派生表达式把工具流水的预算压到 80 token。
2026-09 随 M0.2 整层删除，预算简化为「最近对话 + Checkpoint」两层，
工具流水改用独立配置 ``chat.tool_history_tokens``。

NOTE(M1): 这里的存储仍是进程内 ``OrderedDict``，重启即丢。M1 会把它并入
SQLite 权威存储，届时本模块退化为读缓存。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_SESSIONS = 2000
# 每个会话最多保留的工具调用快照条数（超出淘汰最旧的）
DEFAULT_MAX_TOOL_CALL_ENTRIES = 50


@dataclass
class ToolCallSnapshot:
    """一次工具调用的轻量快照，供下一轮对话回溯「上次查了什么」。"""

    tool_name: str
    arguments_summary: str
    result_summary: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments_summary": self.arguments_summary,
            "result_summary": self.result_summary,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCallSnapshot:
        return cls(
            tool_name=str(data.get("tool_name") or ""),
            arguments_summary=str(data.get("arguments_summary") or ""),
            result_summary=str(data.get("result_summary") or ""),
            timestamp=float(data.get("timestamp") or time.time()),
        )


class SessionSnapshotStore:
    """会话级工具调用流水（进程内，按 LRU 淘汰最久未访问的会话）。

    - key = session_id
    - value = 工具调用快照列表（按时间正序，最旧的在前）
    """

    def __init__(
        self,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_tool_call_entries: int = DEFAULT_MAX_TOOL_CALL_ENTRIES,
    ) -> None:
        self._max_sessions = max_sessions
        self._max_tool_call_entries = max_tool_call_entries
        self._tool_calls: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def clear(self, session_id: str) -> None:
        """清除某会话的全部工具调用流水。"""
        self._tool_calls.pop(session_id, None)

    def append_tool_calls(
        self,
        session_id: str,
        calls: list[ToolCallSnapshot],
        *,
        turn: Any = None,
    ) -> None:
        """追加本轮工具调用快照，超出上限时淘汰最旧的条目。

        :param turn: 定序凭据，进程内存储没有 turn 概念，收下即忽略（协议兼容）
        """
        if not calls:
            return
        bucket = self._tool_calls.setdefault(session_id, [])
        self._tool_calls.move_to_end(session_id)
        bucket.extend(c.to_dict() for c in calls)
        if len(bucket) > self._max_tool_call_entries:
            del bucket[: len(bucket) - self._max_tool_call_entries]
        while len(self._tool_calls) > self._max_sessions:
            self._tool_calls.popitem(last=False)

    def load_tool_calls(self, session_id: str, last_n: int = 10) -> list[ToolCallSnapshot]:
        """加载最近 N 次工具调用快照（按时间正序返回）。"""
        bucket = self._tool_calls.get(session_id) or []
        tail = bucket[-last_n:] if last_n > 0 else list(bucket)
        return [ToolCallSnapshot.from_dict(d) for d in tail]


def format_tool_digest(
    store: Any,
    session_id: str,
    *,
    last_n: int = 12,
    arg_chars: int = 80,
    result_chars: int = 160,
) -> str:
    """把会话最近的工具调用渲染成给压缩器的摘要行。

    Checkpoint 模板要求填「已调用的工具、查询到的核心结果」，但压缩器的输入来自
    ShortTermMemory——那里只有 user/assistant 文本，工具调用在本 store 里，两边
    从不相遇，于是 Checkpoint 只能反复写「无实质工具调用」（见 audit wecom_BoWen）。
    本函数是两者之间的桥。

    与注入上下文的 ``select_tool_history`` 不同：那条路径是查询感知的 Top-N 精选，
    这里要的是「这段时间到底做过什么」的完整流水，故不做相关性筛选、也保留失败项
    ——失败同样是下一轮该知道的状态。

    :param store: 任何实现了 ``load_tool_calls`` 的会话存储
    :param session_id: 会话标识
    :param last_n: 取最近多少次调用
    :param arg_chars: 单条参数摘要的字符上限
    :param result_chars: 单条结果摘要的字符上限
    :returns: 每行一次调用的文本块；无调用或 store 不支持时返回空串
    """
    loader = getattr(store, "load_tool_calls", None)
    if loader is None:
        return ""
    try:
        calls = loader(session_id, last_n=last_n)
    except Exception:  # noqa: BLE001
        return ""
    if not calls:
        return ""

    lines: list[str] = []
    for call in calls:
        args = (call.arguments_summary or "").strip().replace("\n", " ")
        result = (call.result_summary or "").strip().replace("\n", " ")
        if len(args) > arg_chars:
            args = args[:arg_chars] + "…"
        if len(result) > result_chars:
            result = result[:result_chars] + "…"
        parts = [f"- {call.tool_name}"]
        if args:
            parts.append(f"({args})")
        if result:
            parts.append(f" → {result}")
        lines.append("".join(parts))
    return "\n".join(lines)
