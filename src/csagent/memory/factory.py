"""根据配置构造记忆后端。

三种后端，能力不同，不要按「新旧」理解：

- ``sqlite``（默认）：落盘的**权威存储**。一个 store 同时承担模型记忆、界面记录
  与工具流水，重启 / 多 worker 都不丢。见 ``sqlite_store.py``。
- ``memory``：进程内 LRU。零依赖、最快，但重启即失忆，只适合本地开发与测试。
- ``redis``：多实例共享的 TTL 缓存。注意它**解决不了重启失忆**（TTL 一到照样没），
  上它的理由是跨进程共享，不是持久化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from csagent.infra.config import PROJECT_ROOT, Settings
from csagent.memory.protocols import (
    SessionSnapshotStoreProtocol,
    ShortTermMemoryStore,
    TranscriptStore,
)
from csagent.memory.session_snapshot import SessionSnapshotStore
from csagent.memory.short_term import ShortTermMemory

MemoryComponents = tuple[
    ShortTermMemoryStore, SessionSnapshotStoreProtocol, TranscriptStore | None
]


def build_memory_components(
    settings: Settings,
    token_counter: Any = None,
    llm_client: Any = None,
) -> MemoryComponents:
    """返回 ``(short_term, snapshot_store, transcript_store)``。

    第三项只有 ``sqlite`` 后端会给出非 None——那是三者合一的同一个实例。
    其余后端返回 None，由 ``main.py`` 回落到独立的 ``SqliteTranscriptStore``。

    :param settings: 全局配置
    :raises ValueError: ``memory_backend=redis`` 却没配 ``REDIS_URL``
    """
    backend = (settings.memory_backend or "sqlite").strip().lower()

    # ---------- sqlite：落盘权威存储（默认）----------
    if backend == "sqlite":
        from csagent.memory.sqlite_store import SqliteMemoryStore

        store = SqliteMemoryStore(
            _resolve_db_path(settings),
            max_messages=settings.max_history_messages,
            max_sessions=settings.max_sessions,
            max_message_chars=settings.max_message_chars,
            max_return=settings.transcript_max_return,
            episode_gap_seconds=max(0.0, settings.episode_gap_hours) * 3600,
        )
        _migrate_legacy_transcripts(settings, store.path)
        # 同一实例返回三次是刻意的：合并存储的全部意义就是三条读写路径落在同一批行上
        return store, store, store

    # ---------- redis：跨进程共享的 TTL 缓存 ----------
    if backend == "redis":
        if not settings.redis_url:
            raise ValueError("memory_backend=redis 时必须配置 REDIS_URL")
        from csagent.memory.redis_store import (
            RedisSessionSnapshotStore,
            RedisShortTermMemory,
            check_memory_policy,
        )

        short_term_redis = RedisShortTermMemory(
            settings.redis_url,
            max_messages=settings.max_history_messages,
            ttl_seconds=settings.memory_ttl_seconds,
            max_message_chars=settings.max_message_chars,
        )
        snapshots: SessionSnapshotStoreProtocol = RedisSessionSnapshotStore(
            settings.redis_url,
            ttl_seconds=settings.memory_ttl_seconds,
        )
        # Redis 后端没有 max_sessions 这类会话数上限，全局兜底只能靠服务端 maxmemory
        if settings.redis_memory_policy_check:
            check_memory_policy(short_term_redis.client)
        return short_term_redis, snapshots, None

    # ---------- memory：进程内 LRU ----------
    short_term: ShortTermMemoryStore = ShortTermMemory(
        max_messages=settings.max_history_messages,
        max_sessions=settings.max_sessions,
        max_message_chars=settings.max_message_chars,
    )
    return short_term, SessionSnapshotStore(max_sessions=settings.max_sessions), None


def _resolve_db_path(settings: Settings) -> Path:
    """统一记忆库路径；相对路径相对项目根。"""
    path = Path(settings.memory_db_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _migrate_legacy_transcripts(settings: Settings, db_path: Path) -> None:
    """把旧 ``transcripts.db`` 的对话搬进统一库（可重入，目标非空时跳过）。

    放在构造路径上而不是单独的运维脚本：升级后第一次启动就自动完成，
    避免「代码上线了但历史还在旧库里」这种只有翻界面才会发现的状态。
    """
    legacy = Path(settings.transcript_db_path)
    if not legacy.is_absolute():
        legacy = PROJECT_ROOT / legacy
    if legacy == db_path:
        return
    from csagent.memory.sqlite_schema import migrate_from_transcripts

    migrate_from_transcripts(legacy, db_path)
