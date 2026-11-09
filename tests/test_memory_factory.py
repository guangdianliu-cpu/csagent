"""记忆工厂与 Redis 后端导入守卫。"""

from __future__ import annotations

import pytest

from csagent.infra.config import Settings
from csagent.memory.factory import build_memory_components
from csagent.memory.session_snapshot import SessionSnapshotStore
from csagent.memory.short_term import ShortTermMemory
from csagent.memory.sqlite_store import SqliteMemoryStore


def test_build_memory_inprocess_backend(tmp_path):
    """memory 后端：三个 store 各自独立，不提供对话记录（重启即失忆）。"""
    settings = Settings()
    settings.memory_backend = "memory"
    short_term, snapshots, transcript = build_memory_components(settings)
    assert isinstance(short_term, ShortTermMemory)
    assert isinstance(snapshots, SessionSnapshotStore)
    assert transcript is None


def test_build_memory_sqlite_returns_one_unified_store(tmp_path):
    """sqlite 后端：模型记忆 / 工具流水 / 对话记录是**同一个实例**。

    这正是 M1 的核心——三条读写路径落在同一批行上，重启后界面能翻到的
    就是模型 rehydrate 出来的。
    """
    settings = Settings()
    settings.memory_backend = "sqlite"
    settings.memory_db_path = str(tmp_path / "csagent.db")
    settings.transcript_db_path = str(tmp_path / "transcripts.db")

    short_term, snapshots, transcript = build_memory_components(settings)

    assert short_term is snapshots is transcript
    assert isinstance(short_term, SqliteMemoryStore)


def test_sqlite_is_the_default_backend():
    """默认必须是落盘后端：默认值是进程内的话，生产一重启就失忆。"""
    assert Settings().memory_backend == "sqlite"


def test_episode_gap_is_taken_from_config(tmp_path):
    """分段阈值曾经硬编码在 sqlite_schema 里，配置改了也不生效（M2.1）。"""
    settings = Settings()
    settings.memory_backend = "sqlite"
    settings.memory_db_path = str(tmp_path / "csagent.db")
    settings.transcript_db_path = str(tmp_path / "transcripts.db")
    settings.episode_gap_hours = 3

    store, _, _ = build_memory_components(settings)

    assert store._episode_gap == 3 * 3600


def test_build_memory_redis_requires_url():
    settings = Settings()
    settings.memory_backend = "redis"
    settings.redis_url = ""
    with pytest.raises(ValueError, match="REDIS_URL"):
        build_memory_components(settings)


def test_redis_import_error_message():
    """未安装 redis 时构造 Redis 后端应给出明确错误（若已安装则跳过）。"""
    redis_mod = pytest.importorskip("redis", reason="redis optional")
    assert redis_mod is not None
    from csagent.memory.redis_store import RedisShortTermMemory

    # 仅验证类可导入；不连真实 Redis
    assert RedisShortTermMemory.__name__ == "RedisShortTermMemory"


class _FakeRedis:
    """只实现 config_get 的假客户端，用于验证策略探测的分支。"""

    def __init__(self, conf: dict[str, str] | None = None, *, raises: bool = False):
        self._conf = conf or {}
        self._raises = raises

    def config_get(self, pattern: str) -> dict[str, str]:
        if self._raises:
            raise RuntimeError("ERR unknown command 'CONFIG'")
        return {pattern: self._conf.get(pattern, "")}


def _policy_events(conf, *, raises=False):
    pytest.importorskip("redis", reason="redis optional")
    from csagent.memory.redis_store import check_memory_policy

    events: list[tuple[str, dict]] = []

    class _Recorder:
        def __getattr__(self, level):
            def _log(event, **kw):
                events.append((event, kw))

            return _log

    import csagent.memory.redis_store as mod

    original = mod.logger
    mod.logger = _Recorder()
    try:
        check_memory_policy(_FakeRedis(conf, raises=raises))
    finally:
        mod.logger = original
    return [name for name, _ in events]


def test_policy_check_warns_when_maxmemory_unbounded():
    """默认 maxmemory=0 表示不限，会话数只靠 TTL 收敛，必须告警。"""
    assert "redis_maxmemory_unbounded" in _policy_events(
        {"maxmemory": "0", "maxmemory-policy": "noeviction"}
    )


def test_policy_check_warns_on_noeviction_with_maxmemory():
    """设了 maxmemory 却是 noeviction：写满后写入报 OOM，比崩更难查。"""
    assert "redis_eviction_policy_unsafe" in _policy_events(
        {"maxmemory": "2147483648", "maxmemory-policy": "noeviction"}
    )


def test_policy_check_accepts_volatile_lru():
    assert "redis_memory_policy_ok" in _policy_events(
        {"maxmemory": "2147483648", "maxmemory-policy": "volatile-lru"}
    )


def test_policy_check_skips_when_config_command_unavailable():
    """托管 Redis 常禁用 CONFIG——探测失败不该拦住启动。"""
    assert _policy_events({}, raises=True) == ["redis_memory_policy_check_skipped"]


def test_policy_check_warns_when_redis_unreachable():
    """连不上和「CONFIG 被禁用」是两回事：前者说明整个记忆后端是坏的。"""
    redis_mod = pytest.importorskip("redis", reason="redis optional")

    class _Unreachable:
        def config_get(self, pattern):
            raise redis_mod.exceptions.ConnectionError("Connection refused")

    from csagent.memory.redis_store import check_memory_policy
    import csagent.memory.redis_store as mod

    events = []

    class _Recorder:
        def __getattr__(self, level):
            return lambda event, **kw: events.append(event)

    original = mod.logger
    mod.logger = _Recorder()
    try:
        check_memory_policy(_Unreachable())
    finally:
        mod.logger = original

    assert events == ["redis_unreachable_at_startup"]
