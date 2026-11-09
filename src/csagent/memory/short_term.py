"""会话内短期记忆（MVP 使用进程内存储）。"""

from __future__ import annotations

from collections import OrderedDict

from csagent.react.models import ChatMessage

# 单条消息写入时的默认字符上限。见 clip_message 的取值说明。
DEFAULT_MAX_MESSAGE_CHARS = 8000

_TRUNCATION_MARK = "…（已截断）"


def clip_message(text: str, max_chars: int) -> str:
    """按字符上限截断单条消息，超长时追加显式标记。

    为什么截断发生在**写入**而不是只在读取时做：读取侧（``StructuredCompactor``）
    确实会按 Token 预算截断，但那只影响进模型的文本，存储里留的仍是全文——
    一条几十 KB 的长回复 × 20 条窗口，单会话 key 就能到 MB 级，
    而 Redis 后端没有会话数上限（只有 TTL），量一上来就是纯粹的内存浪费。

    上限必须 >= 读取侧 ``recent_latest_max_tokens``（末条消息的防御性天花板，
    8000 token）能吃下的量，否则会截掉模型本来读得到的内容。中文约 1 字 >= 1 token，
    默认 8000 字符即已覆盖；英文占比高的流量（约 4 字符/token）需上调。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_MARK


class ShortTermMemory:
    """单次会话消息历史，超出上限时裁剪最早的消息，并按 LRU 淘汰会话防止内存无界增长。"""

    def __init__(
        self,
        max_messages: int = 20,
        max_sessions: int = 2000,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    ) -> None:
        self._max_messages = max_messages
        self._max_sessions = max_sessions
        self._max_message_chars = max_message_chars
        self._sessions: OrderedDict[str, list[ChatMessage]] = OrderedDict()

    def load(self, session_id: str) -> list[ChatMessage]:
        """加载指定会话的历史消息，并标记为最近访问。"""
        history = self._sessions.get(session_id)
        if history is None:
            return []
        self._sessions.move_to_end(session_id)
        return list(history)

    def append(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """追加一轮对话并裁剪历史长度，超过会话数上限时淘汰最久未使用的会话。"""
        history = self._sessions.setdefault(session_id, [])
        self._sessions.move_to_end(session_id)
        history.append(
            ChatMessage(role="user", content=clip_message(user_text, self._max_message_chars))
        )
        history.append(
            ChatMessage(
                role="assistant", content=clip_message(assistant_text, self._max_message_chars)
            )
        )
        if len(history) > self._max_messages:
            self._sessions[session_id] = history[-self._max_messages :]

        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
