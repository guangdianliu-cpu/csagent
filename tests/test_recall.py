"""跨 episode FTS5 召回（M3.4）测试。"""

from __future__ import annotations

import time
from contextlib import closing

import pytest

from csagent.memory.artifact_store import ArtifactStore
from csagent.memory.recall import EpisodeRecaller, _looks_time_pointing
from csagent.memory.sqlite_fts import fts5_available
from csagent.memory.sqlite_schema import connect
from csagent.memory.sqlite_store import SqliteMemoryStore


def _recaller(tmp_path, **kwargs) -> tuple[SqliteMemoryStore, EpisodeRecaller]:
    store = SqliteMemoryStore(tmp_path / "csagent.db")
    with closing(connect(store.path)) as conn:
        if not fts5_available(conn):
            pytest.skip("当前 SQLite 未编译 FTS5")
    return store, EpisodeRecaller(store.path, **kwargs)


def _age_episode(path, episode_id: str, seconds: float) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "UPDATE episodes SET last_active = last_active - ? WHERE id = ?",
            (seconds, episode_id),
        )
        conn.execute(
            "UPDATE messages SET created_at = created_at - ? WHERE episode_id = ?",
            (seconds, episode_id),
        )


def test_time_pointing_detects_explicit_dates():
    assert _looks_time_pointing("去年那次查过什么")
    assert _looks_time_pointing("2025 年 3 月的文件")
    assert not _looks_time_pointing("接着刚才那个")


def test_recall_finds_older_episode_and_stamps_date(tmp_path):
    store, recaller = _recaller(tmp_path, window_days=180)
    store.append_turn(
        "web:u1",
        user_text="请查 ALPHA01 的资料",
        assistant_text="ALPHA01 已查过，详见工具记录",
    )
    old_ep = store.current_episode_id("web:u1")
    _age_episode(store.path, old_ep, 13 * 3600)

    store.append_turn("web:u1", user_text="今天的新问题", assistant_text="新答案")
    new_ep = store.current_episode_id("web:u1")
    assert new_ep != old_ep

    block = recaller.format_block(
        "web:u1", "ALPHA01 上次查的结果", exclude_episode_id=new_ep
    )
    assert "ALPHA01" in block
    assert "跨段召回" in block
    # 带日期前缀
    assert "[" in block and "]" in block


def test_recall_default_window_hides_very_old_hits(tmp_path):
    store, recaller = _recaller(tmp_path, window_days=10)
    store.append_turn(
        "web:u1", user_text="ALPHA01 很久以前", assistant_text="记下了 ALPHA01"
    )
    old_ep = store.current_episode_id("web:u1")
    _age_episode(store.path, old_ep, 20 * 86400)

    store.append_turn("web:u1", user_text="新问题", assistant_text="新答案")
    new_ep = store.current_episode_id("web:u1")
    hits = recaller.search(
        "web:u1", "ALPHA01", exclude_episode_id=new_ep, now=time.time()
    )
    assert hits == []

    # 问句带时间指向则放开
    hits = recaller.search(
        "web:u1", "去年 ALPHA01", exclude_episode_id=new_ep, now=time.time()
    )
    assert any("ALPHA01" in h.text for h in hits)


def test_artifact_filename_is_searchable(tmp_path):
    store, recaller = _recaller(tmp_path)
    store.append_turn("web:u1", user_text="先占位", assistant_text="ok")
    old_ep = store.current_episode_id("web:u1")
    artifacts = ArtifactStore(store.path)
    artifacts.register(
        session_id="web:u1",
        kind="outbound",
        path=str(tmp_path / "report-ALPHA01.xlsx"),
        filename="report-ALPHA01.xlsx",
        episode_id=old_ep,
        size=12,
        compute_hash=False,
    )
    _age_episode(store.path, old_ep, 13 * 3600)

    store.append_turn("web:u1", user_text="新段", assistant_text="新")
    new_ep = store.current_episode_id("web:u1")
    hits = recaller.search(
        "web:u1", "ALPHA01", exclude_episode_id=new_ep, now=time.time()
    )
    assert any(h.kind == "artifact" and "ALPHA01" in h.text for h in hits)
