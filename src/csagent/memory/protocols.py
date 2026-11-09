"""记忆存储协议：进程内 / Redis 等实现可互换。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from csagent.react.models import ChatMessage


# ---------- 并发定序（M2.2）----------


@dataclass(frozen=True)
class TurnHandle:
    """一轮对话的定序凭据：请求进入时预留，回写时按它落位。

    存在的理由是**同一身份的并行提问**。企微侧本来就能连发两条，两条会各跑各的
    ReAct 循环。若 ``turn_id`` 留到回写时才用 ``MAX(turn_id)+1`` 算，排序依据就变成
    了「谁先跑完」——先问的那句如果多调了两个工具，翻历史时会排在后问的那句后面，
    读起来像是答非所问。预留改成在入口处做，顺序就锁定在「谁先问」。

    ``started_at`` 同时充当 ``messages.created_at``：一轮的时间应当是**用户提问的
    时刻**，不是助手写完的时刻。读取侧按它排序，因此它也是跨 episode 唯一有效的
    全局序——``turn_id`` 每段从头计数，单独拿来排序会把不同 episode 的轮次交错。

    :param session_id: 所属会话
    :param episode_id: 本轮落在哪一段时间线上
    :param turn_id: 该 episode 内的到达序号，从 1 开始
    :param started_at: 请求到达时刻（Unix 秒）
    """

    session_id: str
    episode_id: str
    turn_id: int
    started_at: float


@runtime_checkable
class ShortTermMemoryStore(Protocol):
    def load(self, session_id: str) -> list[ChatMessage]: ...

    def append(self, session_id: str, user_text: str, assistant_text: str) -> None: ...


@runtime_checkable
class TranscriptStore(Protocol):
    """对话记录（给界面翻历史用，不进模型上下文）。见 ``transcript.py`` 的分工说明。"""

    def append(
        self, session_id: str, user_text: str, assistant_text: str, *, channel: str = ""
    ) -> None: ...

    def load_recent(self, session_id: str, limit: int | None = None) -> list[ChatMessage]: ...

    def purge_older_than(self, days: float) -> int: ...


@runtime_checkable
class SessionSnapshotStoreProtocol(Protocol):
    """会话级工具调用流水。见 ``session_snapshot.py`` 关于领域快照层已删除的说明。"""

    def clear(self, session_id: str) -> None: ...

    def append_tool_calls(
        self, session_id: str, calls: list[Any], *, turn: TurnHandle | None = None
    ) -> None:
        """``turn`` 为本轮的定序凭据；不记录 turn 的实现（进程内 / Redis）忽略它。"""
        ...

    def load_tool_calls(self, session_id: str, last_n: int = 10) -> list[Any]: ...
