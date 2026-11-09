"""Redis 后端的短期记忆与会话快照（可选依赖 redis）。

未安装 redis 或未配置 REDIS_URL 时不要选用本模块；默认仍用进程内实现。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from csagent.memory.session_snapshot import ToolCallSnapshot
from csagent.memory.short_term import DEFAULT_MAX_MESSAGE_CHARS, clip_message
from csagent.react.models import ChatMessage

try:
    import redis
except ImportError:  # pragma: no cover - 可选依赖
    redis = None  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

# 可接受的驱逐策略：所有 key 都带 TTL，volatile-* 语义正好；
# allkeys-* 也能兜底，只是会连带淘汰未来可能放进同一 db 的无 TTL 数据。
_SAFE_EVICTION_POLICIES = {
    "volatile-lru",
    "volatile-lfu",
    "volatile-random",
    "volatile-ttl",
    "allkeys-lru",
    "allkeys-lfu",
    "allkeys-random",
}


def _require_redis():
    if redis is None:
        raise ImportError(
            "Redis 记忆后端需要安装 redis 包：pip install 'redis>=5' 或 cs-agent[redis]"
        )
    return redis


def _make_client(redis_url: str):
    """连接 Redis；强制 RESP2，兼容不支持 HELLO 的 5.x 服务端。"""
    r = _require_redis()
    return r.Redis.from_url(redis_url, decode_responses=True, protocol=2)


def check_memory_policy(client) -> None:
    """启动时探测 Redis 的内存上限与驱逐策略，配置不安全时告警。

    应用侧只能限制**单个会话**写多少（消息条数、单条字符数、快照字符数），
    会话总数是没有上限的——Redis 后端不像进程内后端那样有 ``max_sessions`` LRU，
    只靠 TTL 过期。所以全局兜底必须落在 Redis 自己的 maxmemory 上：

    - ``maxmemory=0``（默认）：不设上限，内存一路涨到宿主机 OOM。
    - ``maxmemory>0`` 但 ``maxmemory-policy=noeviction``（默认）：写满后写入直接
      报 OOM 错误，表现是会话读得到却写不进去，比直接崩更难查。

    托管 Redis 常禁用 CONFIG 命令，此时静默跳过——探测失败不该拦住启动。
    """
    try:
        conf = client.config_get("maxmemory")
        conf.update(client.config_get("maxmemory-policy"))
    except Exception as exc:  # noqa: BLE001 - 探测失败一律不拦启动
        # 连不上和「CONFIG 被禁用」是两回事：后者正常（托管 Redis 常见），
        # 前者说明整个记忆后端都是坏的，值得一条 warning 而不是静默 debug。
        conn_error = getattr(getattr(redis, "exceptions", None), "ConnectionError", ())
        if conn_error and isinstance(exc, conn_error):
            logger.warning("redis_unreachable_at_startup", error=str(exc))
        else:
            logger.debug("redis_memory_policy_check_skipped", error=str(exc))
        return

    try:
        maxmemory = int(conf.get("maxmemory") or 0)
    except (TypeError, ValueError):
        maxmemory = 0
    policy = str(conf.get("maxmemory-policy") or "noeviction").strip().lower()

    if maxmemory <= 0:
        logger.warning(
            "redis_maxmemory_unbounded",
            policy=policy,
            hint=(
                "Redis 未设 maxmemory，会话数无上限、仅靠 TTL 过期，内存会涨到宿主机 OOM。"
                "建议 redis.conf 设置 maxmemory <预算> 且 maxmemory-policy volatile-lru"
            ),
        )
    elif policy not in _SAFE_EVICTION_POLICIES:
        logger.warning(
            "redis_eviction_policy_unsafe",
            maxmemory=maxmemory,
            policy=policy,
            hint=(
                f"maxmemory 已设但 policy={policy}，写满后会话将无法写入（OOM 报错）。"
                "建议改为 volatile-lru"
            ),
        )
    else:
        logger.info("redis_memory_policy_ok", maxmemory=maxmemory, policy=policy)


class RedisShortTermMemory:
    """会话消息历史存 Redis List；TTL 控制过期。"""

    def __init__(
        self,
        redis_url: str,
        *,
        max_messages: int = 20,
        key_prefix: str = "csagent:stm:",
        ttl_seconds: int = 86400,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    ) -> None:
        self._client = _make_client(redis_url)
        self._max_messages = max_messages
        self._prefix = key_prefix
        self._ttl = ttl_seconds
        self._max_message_chars = max_message_chars

    @property
    def client(self):
        """暴露底层连接，供启动期的 maxmemory 策略探测复用（避免多开一条连接）。"""
        return self._client

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def load(self, session_id: str) -> list[ChatMessage]:
        raw_list = self._client.lrange(self._key(session_id), 0, -1)
        messages: list[ChatMessage] = []
        for item in raw_list:
            try:
                data = json.loads(item)
                messages.append(ChatMessage(role=data["role"], content=data["content"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return messages

    def append(self, session_id: str, user_text: str, assistant_text: str) -> None:
        key = self._key(session_id)
        pipe = self._client.pipeline()
        limit = self._max_message_chars
        pipe.rpush(
            key,
            json.dumps(
                {"role": "user", "content": clip_message(user_text, limit)},
                ensure_ascii=False,
            ),
            json.dumps(
                {"role": "assistant", "content": clip_message(assistant_text, limit)},
                ensure_ascii=False,
            ),
        )
        pipe.ltrim(key, -self._max_messages, -1)
        pipe.expire(key, self._ttl)
        pipe.execute()


class RedisSessionSnapshotStore:
    """会话级工具调用流水存 Redis List（key 前缀 ``csagent:snap:tools:``）。

    领域快照 Hash（``csagent:snap:{sid}``）已随 M0.2 删除，原因见
    ``session_snapshot.py`` 的模块注释。**旧 key 不会被本类清理**，
    切到 redis 后端前可手动 ``SCAN`` + ``DEL`` 一次遗留的 ``csagent:snap:*``
    （不删也只是占内存，带 TTL 会自然过期）。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        tool_call_key_prefix: str = "csagent:snap:tools:",
        max_tool_call_entries: int = 50,
        ttl_seconds: int = 86400,
    ) -> None:
        self._client = _make_client(redis_url)
        self._tool_prefix = tool_call_key_prefix
        self._max_tool_call_entries = max_tool_call_entries
        self._ttl = ttl_seconds

    def _tool_key(self, session_id: str) -> str:
        return f"{self._tool_prefix}{session_id}"

    def clear(self, session_id: str) -> None:
        self._client.delete(self._tool_key(session_id))

    def append_tool_calls(
        self,
        session_id: str,
        calls: list[ToolCallSnapshot],
        *,
        turn: Any = None,
    ) -> None:
        """追加本轮工具调用快照，存为 Redis List 并按上限裁剪。

        :param turn: 定序凭据，Redis 后端不记录 turn，收下即忽略（协议兼容）
        """
        if not calls:
            return
        key = self._tool_key(session_id)
        pipe = self._client.pipeline()
        for call in calls:
            pipe.rpush(key, json.dumps(call.to_dict(), ensure_ascii=False, default=str))
        pipe.ltrim(key, -self._max_tool_call_entries, -1)
        pipe.expire(key, self._ttl)
        pipe.execute()

    def load_tool_calls(self, session_id: str, last_n: int = 10) -> list[ToolCallSnapshot]:
        """加载最近 N 次工具调用快照（按时间正序返回）。"""
        raw_list = self._client.lrange(self._tool_key(session_id), -last_n, -1) if last_n > 0 else (
            self._client.lrange(self._tool_key(session_id), 0, -1)
        )
        calls: list[ToolCallSnapshot] = []
        for item in raw_list:
            try:
                calls.append(ToolCallSnapshot.from_dict(json.loads(item)))
            except (json.JSONDecodeError, TypeError):
                continue
        return calls
