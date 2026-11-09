"""结构化压缩器缓存后端：支持内存 LRU 与 Redis 共享存储。"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class SessionSummaryRecord:
    """会话压缩 Checkpoint 记录。"""

    summary: str = ""
    covered_digests: list[str] = field(default_factory=list)
    covered_rounds: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSummaryRecord:
        return cls(
            summary=str(data.get("summary") or ""),
            covered_digests=list(data.get("covered_digests") or []),
            covered_rounds=int(data.get("covered_rounds") or 0),
            updated_at=float(data.get("updated_at") or time.time()),
        )


@runtime_checkable
class CompactorCacheBackend(Protocol):
    """压缩器缓存协议。"""

    def get(self, session_id: str) -> SessionSummaryRecord | None: ...

    def set(self, session_id: str, record: SessionSummaryRecord) -> None: ...

    def delete(self, session_id: str) -> None: ...


class InMemoryCompactorCache:
    """进程内 LRU 缓存。"""

    def __init__(self, max_sessions: int = 2000) -> None:
        self._max_sessions = max_sessions
        self._cache: OrderedDict[str, SessionSummaryRecord] = OrderedDict()

    def get(self, session_id: str) -> SessionSummaryRecord | None:
        record = self._cache.get(session_id)
        if record is not None:
            self._cache.move_to_end(session_id)
        return record

    def set(self, session_id: str, record: SessionSummaryRecord) -> None:
        self._cache[session_id] = record
        self._cache.move_to_end(session_id)
        while len(self._cache) > self._max_sessions:
            self._cache.popitem(last=False)

    def delete(self, session_id: str) -> None:
        self._cache.pop(session_id, None)


class RedisCompactorCache:
    """Redis 分布式缓存（支持多 Worker 实例共享 Checkpoint）。"""

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = "csagent:summary:",
        ttl_seconds: int = 86400,
    ) -> None:
        from csagent.memory.redis_store import _make_client

        self._client = _make_client(redis_url)
        self._prefix = key_prefix
        self._ttl = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def get(self, session_id: str) -> SessionSummaryRecord | None:
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return SessionSummaryRecord.from_dict(data)
        except Exception as exc:
            logger.warning("redis_compactor_cache_read_failed", session_id=session_id, error=str(exc))
            return None

    def set(self, session_id: str, record: SessionSummaryRecord) -> None:
        try:
            key = self._key(session_id)
            payload = json.dumps(record.to_dict(), ensure_ascii=False)
            self._client.set(key, payload, ex=self._ttl)
        except Exception as exc:
            logger.warning("redis_compactor_cache_write_failed", session_id=session_id, error=str(exc))

    def delete(self, session_id: str) -> None:
        try:
            self._client.delete(self._key(session_id))
        except Exception:
            pass
