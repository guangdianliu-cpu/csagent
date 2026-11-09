"""统一持久化记忆层（M1）测试。

核心主张只有一条：**进程重启后，模型 rehydrate 出来的历史和界面翻到的是同一批行**。
原先模型记忆在进程内 OrderedDict、界面记录在 transcripts.db，重启后前者清空、
后者还在，Agent 会对用户断言「本会话没发过文件」。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from csagent.memory.session_snapshot import ToolCallSnapshot
from csagent.memory.sqlite_schema import connect, ensure_schema, migrate_from_transcripts
from csagent.memory.sqlite_store import SqliteMemoryStore


def _store(tmp_path, **kwargs) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "csagent.db", **kwargs)


# ---------------------------------------------------------------- 重启不失忆


def test_history_survives_process_restart(tmp_path):
    """换一个 store 实例（等价于进程重启）后，模型窗口应原样读回。"""
    first = _store(tmp_path)
    first.append_turn(
        "web:u1", user_text="帮我看下这个文件", assistant_text="看完了，结论是 A"
    )

    reborn = _store(tmp_path)  # 新实例、空缓存
    history = reborn.load("web:u1")

    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "帮我看下这个文件"
    assert history[1].content == "看完了，结论是 A"


def test_tool_calls_survive_process_restart(tmp_path):
    first = _store(tmp_path)
    first.append_turn("web:u1", user_text="查一下", assistant_text="查好了")
    first.append_tool_calls(
        "web:u1",
        [ToolCallSnapshot(tool_name="run_script", arguments_summary="a.py", result_summary="命中 11 条")],
    )

    reborn = _store(tmp_path)
    calls = reborn.load_tool_calls("web:u1")

    assert len(calls) == 1
    assert calls[0].tool_name == "run_script"
    assert calls[0].result_summary == "命中 11 条"


def test_load_window_is_capped_and_ordered(tmp_path):
    """窗口按 max_messages 截断，且按时间正序返回。"""
    store = _store(tmp_path, max_messages=4)
    for i in range(5):
        store.append_turn("web:u1", user_text=f"q{i}", assistant_text=f"a{i}")

    history = _store(tmp_path, max_messages=4).load("web:u1")

    assert [m.content for m in history] == ["q3", "a3", "q4", "a4"]


# ---------------------------------------------------------------- 两种文本


def test_model_text_and_display_text_are_kept_apart(tmp_path):
    """富文本喂模型、人话给界面，合并存储后两者不能互相污染。"""
    store = _store(tmp_path)
    rich = "分析一下\n\n[附件] 报表.xlsx\n路径: data/uploads/s1/报表.xlsx\n预览: ..."
    store.append_turn(
        "web:u1",
        user_text=rich,
        assistant_text="已生成图表",
        display_user_text="分析一下 [附件] 报表.xlsx",
        display_assistant_text="已生成图表",
        user_files=[{"filename": "报表.xlsx", "workspace_path": "data/uploads/s1/报表.xlsx"}],
    )

    model_side = store.load("web:u1")
    ui_side = store.load_recent_with_files("web:u1")

    assert "路径: data/uploads" in model_side[0].content, "模型侧必须保留工作区路径"
    assert ui_side[0]["content"] == "分析一下 [附件] 报表.xlsx"
    assert "路径: data/uploads" not in ui_side[0]["content"], "界面不该被富文本糊满"
    assert ui_side[0]["files"][0]["filename"] == "报表.xlsx"


def test_display_falls_back_to_content_when_identical(tmp_path):
    """没有单独的界面口径时，读取侧回落富文本，而不是返回空串。"""
    store = _store(tmp_path)
    store.append_turn("web:u1", user_text="你好", assistant_text="您好")

    assert store.load_recent_with_files("web:u1")[0]["content"] == "你好"


def test_turn_with_only_a_file_is_recorded(tmp_path):
    """用户只拖了个文件、一个字没打，同样是一轮交互。"""
    store = _store(tmp_path)
    store.append_turn(
        "web:u1", user_text="", assistant_text="收到",
        user_files=[{"filename": "a.xlsx", "workspace_path": "p/a.xlsx"}],
    )

    assert len(store.load_recent_with_files("web:u1")) == 2


# ---------------------------------------------------------------- 时间线分段


def _age_episode(path, session_id: str, seconds: float) -> None:
    """把该会话最新 episode 的 last_active 往前拨，模拟静默了这么久。"""
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "UPDATE episodes SET last_active = last_active - ? WHERE session_id = ?",
            (seconds, session_id),
        )


def test_turn_id_increments_within_one_episode(tmp_path):
    store = _store(tmp_path)
    first = store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    second = store.append_turn("web:u1", user_text="q2", assistant_text="a2")

    assert (first, second) == (1, 2)


def test_load_only_returns_current_episode(tmp_path):
    """M3.4：模型窗口只看当前段；界面翻历史仍能看到上一段。"""
    store = _store(tmp_path, episode_gap_seconds=3600)
    store.append_turn("web:u1", user_text="昨天的问题", assistant_text="昨天的答案")
    _age_episode(store.path, "web:u1", 7200)
    store.append_turn("web:u1", user_text="今天的问题", assistant_text="今天的答案")

    model_side = store.load("web:u1")
    ui_side = [m.content for m in store.load_recent("web:u1")]

    assert [m.content for m in model_side] == ["今天的问题", "今天的答案"]
    assert "昨天的问题" in ui_side
    assert "今天的问题" in ui_side


def test_cache_drops_when_episode_changes(tmp_path):
    """先 load 建立上一段缓存，开新段后窗口必须换成新段，不能把旧原文续灌。"""
    store = _store(tmp_path, episode_gap_seconds=3600)
    store.append_turn("web:u1", user_text="旧问", assistant_text="旧答")
    store.load("web:u1")

    _age_episode(store.path, "web:u1", 7200)
    store.append_turn("web:u1", user_text="新问", assistant_text="新答")

    assert [m.content for m in store.load("web:u1")] == ["新问", "新答"]


def test_long_silence_starts_a_new_episode(tmp_path):
    """静默超过 episode_gap 后开新段，turn_id 在段内重新计数。"""
    store = _store(tmp_path, episode_gap_seconds=3600)
    store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    before = store.current_episode_id("web:u1")

    _age_episode(store.path, "web:u1", 7200)
    turn_id = store.append_turn("web:u1", user_text="q2", assistant_text="a2")
    after = store.current_episode_id("web:u1")

    assert after != before
    assert after.endswith("#1")
    assert turn_id == 1, "新 episode 的 turn_id 应从 1 重新开始"


def test_short_silence_stays_in_same_episode(tmp_path):
    store = _store(tmp_path, episode_gap_seconds=3600)
    store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    before = store.current_episode_id("web:u1")

    _age_episode(store.path, "web:u1", 60)
    store.append_turn("web:u1", user_text="q2", assistant_text="a2")

    assert store.current_episode_id("web:u1") == before


def test_sessions_do_not_share_episodes_or_history(tmp_path):
    store = _store(tmp_path)
    store.append_turn("web:u1", user_text="我的问题", assistant_text="我的答案")
    store.append_turn("wecom:g2", user_text="别人的问题", assistant_text="别人的答案")

    assert [m.content for m in store.load("web:u1")] == ["我的问题", "我的答案"]
    assert store.current_episode_id("web:u1") != store.current_episode_id("wecom:g2")


# ---------------------------------------------------------------- 并发定序（M2.2）


def test_turn_number_is_taken_at_arrival_not_at_write(tmp_path):
    """先问的那轮即使后写完，也要排在前面。

    这是 M2.2 的全部理由：同一个人并发提两个问题，先问的那句如果多调了两个工具，
    回写就会晚于后问的那句。按落库顺序排，翻历史会看到答非所问。
    """
    store = _store(tmp_path)

    first = store.begin_turn("web:u1")   # 先到
    second = store.begin_turn("web:u1")  # 后到
    # 后到的先跑完
    store.append_turn("web:u1", user_text="第二个问题", assistant_text="快答", turn=second)
    store.append_turn("web:u1", user_text="第一个问题", assistant_text="慢答", turn=first)

    assert (first.turn_id, second.turn_id) == (1, 2)
    assert [m.content for m in store.load("web:u1")] == [
        "第一个问题", "慢答", "第二个问题", "快答",
    ]


def test_parallel_reservations_never_share_a_number(tmp_path):
    """号源必须能在单事务里自增，不能从已有行推算。

    并行拿号的那一刻两轮的 messages 都还没落库，``MAX(turn_id)+1`` 会让两者拿到
    同一个号——回写时互相盖掉一半。
    """
    store = _store(tmp_path)

    handles = [store.begin_turn("web:u1") for _ in range(5)]

    assert [h.turn_id for h in handles] == [1, 2, 3, 4, 5]
    assert len({h.episode_id for h in handles}) == 1, "同一段静默期内不该分裂 episode"


def test_threaded_begin_turn_never_drops_or_collides(tmp_path):
    """多线程同时 begin_turn 不得返回 None，也不得撞号。

    单事件循环里 begin_turn 是同步的、天然串行；企微多 worker / 线程才会真抢锁。
    DEFERRED 事务在 WAL 下会 SQLITE_BUSY_SNAPSHOT，busy_timeout 不救，号就丢了。
    """
    from concurrent.futures import ThreadPoolExecutor

    store = _store(tmp_path)
    n = 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = list(pool.map(lambda _: store.begin_turn("web:u1"), range(n)))

    assert all(h is not None for h in handles)
    ids = [h.turn_id for h in handles]
    assert len(set(ids)) == n
    assert sorted(ids) == list(range(1, n + 1))


def test_reserved_turn_is_reused_not_reallocated_on_write(tmp_path):
    """带凭据回写不应再取一次号，否则号会白白跳空。"""
    store = _store(tmp_path)
    handle = store.begin_turn("web:u1")

    written = store.append_turn("web:u1", user_text="q", assistant_text="a", turn=handle)
    following = store.begin_turn("web:u1")

    assert written == handle.turn_id == 1
    assert following.turn_id == 2


def test_tool_calls_land_on_the_current_turn(tmp_path):
    """工具流水写在本轮 messages **之前**，没有凭据就只能挂到上一轮去。"""
    store = _store(tmp_path)
    store.append_turn("web:u1", user_text="上一轮", assistant_text="上一轮答")

    handle = store.begin_turn("web:u1")
    store.append_tool_calls(
        "web:u1",
        [ToolCallSnapshot(tool_name="run_script", arguments_summary="a.py", result_summary="ok")],
        turn=handle,
    )

    with closing(connect(store.path)) as conn:
        turn_id = conn.execute("SELECT turn_id FROM tool_calls").fetchone()[0]

    assert turn_id == handle.turn_id == 2


def test_begin_turn_opens_a_new_episode_after_long_silence(tmp_path):
    """分段判断挪到拿号这一步之后，跨段行为必须不变。"""
    store = _store(tmp_path, episode_gap_seconds=3600)
    store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    before = store.current_episode_id("web:u1")

    _age_episode(store.path, "web:u1", 7200)
    handle = store.begin_turn("web:u1")

    assert handle.episode_id != before
    assert handle.turn_id == 1


def test_begin_turn_returns_none_for_empty_session(tmp_path):
    assert _store(tmp_path).begin_turn("") is None


# ---------------------------------------------------------------- 中断标记


def test_interrupted_flag_is_stored_on_assistant_row(tmp_path):
    """M0.1 的中断轮次要能在库里区分出来，否则事后无从判断回答为何截断。"""
    store = _store(tmp_path)
    store.append_turn(
        "web:u1", user_text="q", assistant_text="说到一半", interrupted=True
    )

    with closing(connect(store.path)) as conn:
        rows = conn.execute(
            "SELECT role, interrupted FROM messages ORDER BY id"
        ).fetchall()

    assert rows == [("user", 0), ("assistant", 1)]


# ---------------------------------------------------------------- 缓存一致性


def test_cache_reflects_writes_made_after_first_read(tmp_path):
    """先 load 建立缓存，再 append，下一次 load 必须看得到新轮次。"""
    store = _store(tmp_path)
    store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    store.load("web:u1")  # 建立 L1 缓存

    store.append_turn("web:u1", user_text="q2", assistant_text="a2")

    assert [m.content for m in store.load("web:u1")] == ["q1", "a1", "q2", "a2"]


def test_cache_and_database_agree_on_out_of_order_writes(tmp_path):
    """乱序落库时，读缓存和读库必须给出同一个顺序。

    并行的两轮按「谁先跑完」落库、按「谁先问」排序，所以后写的那轮可能要插到中间。
    缓存只按追加顺序排的话，同一个会话在重启前后会看到两种顺序——这是最难查的
    一类 bug：谁都不会怀疑「缓存」能改变历史的先后。
    """
    store = _store(tmp_path)
    first = store.begin_turn("web:u1")
    second = store.begin_turn("web:u1")

    store.append_turn("web:u1", user_text="后问", assistant_text="后答", turn=second)
    store.load("web:u1")  # 建立缓存，此刻里面只有「后问」
    store.append_turn("web:u1", user_text="先问", assistant_text="先答", turn=first)

    from_cache = [m.content for m in store.load("web:u1")]
    from_db = [m.content for m in _store(tmp_path).load("web:u1")]  # 新实例，空缓存

    assert from_cache == from_db == ["先问", "先答", "后问", "后答"]


def test_cache_eviction_falls_back_to_database(tmp_path):
    """缓存被 LRU 淘汰后，load 应回库重建而不是返回空。"""
    store = _store(tmp_path, max_sessions=1)
    store.append_turn("web:u1", user_text="q1", assistant_text="a1")
    store.load("web:u1")
    store.append_turn("web:u2", user_text="q2", assistant_text="a2")
    store.load("web:u2")  # 把 u1 挤出缓存

    assert [m.content for m in store.load("web:u1")] == ["q1", "a1"]


# ---------------------------------------------------------------- 旧库迁移


def _make_legacy_db(path, rows: list[tuple]) -> None:
    """造一个旧版 transcripts.db。"""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,"
            " channel TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,"
            " content TEXT NOT NULL, created_at REAL NOT NULL,"
            " attachments TEXT NOT NULL DEFAULT '')"
        )
        conn.executemany(
            "INSERT INTO messages (session_id, channel, role, content, created_at, attachments)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_migration_moves_legacy_conversations(tmp_path):
    legacy = tmp_path / "transcripts.db"
    _make_legacy_db(legacy, [
        ("web:u1", "web", "user", "老问题", 1000.0, ""),
        ("web:u1", "web", "assistant", "老答案", 1001.0, ""),
    ])
    db = tmp_path / "csagent.db"
    ensure_schema(db)

    moved = migrate_from_transcripts(legacy, db)

    assert moved == 2
    assert [m.content for m in SqliteMemoryStore(db).load("web:u1")] == ["老问题", "老答案"]


def test_migration_is_reentrant(tmp_path):
    """迁移挂在启动路径上，第二次启动不能把数据搬第二遍。"""
    legacy = tmp_path / "transcripts.db"
    _make_legacy_db(legacy, [("web:u1", "web", "user", "老问题", 1000.0, "")])
    db = tmp_path / "csagent.db"
    ensure_schema(db)

    assert migrate_from_transcripts(legacy, db) == 1
    assert migrate_from_transcripts(legacy, db) == 0
    assert len(SqliteMemoryStore(db).load("web:u1")) == 1


def test_migration_splits_legacy_timeline_into_episodes(tmp_path):
    """旧记录按时间间隔重新切段，而不是全部塞进 episode 0。"""
    legacy = tmp_path / "transcripts.db"
    day = 86400.0
    _make_legacy_db(legacy, [
        ("web:u1", "web", "user", "第一天", 1000.0, ""),
        ("web:u1", "web", "assistant", "回复", 1001.0, ""),
        ("web:u1", "web", "user", "三天后", 1000.0 + 3 * day, ""),
    ])
    db = tmp_path / "csagent.db"
    ensure_schema(db)
    migrate_from_transcripts(legacy, db)

    with closing(connect(db)) as conn:
        episodes = conn.execute(
            "SELECT id FROM episodes WHERE session_id = 'web:u1' ORDER BY seq"
        ).fetchall()

    assert len(episodes) == 2


def test_migration_skips_when_legacy_db_is_absent(tmp_path):
    db = tmp_path / "csagent.db"
    ensure_schema(db)
    assert migrate_from_transcripts(tmp_path / "nope.db", db) == 0


def test_migration_survives_corrupt_legacy_db(tmp_path):
    """旧库损坏不能挡住服务启动。"""
    legacy = tmp_path / "transcripts.db"
    legacy.write_bytes(b"not a database at all")
    db = tmp_path / "csagent.db"
    ensure_schema(db)

    assert migrate_from_transcripts(legacy, db) == 0


def test_migrated_episode_keeps_counting_where_history_left_off(tmp_path):
    """迁移进来的历史已经占了 turn_id，号源必须接着往下走而不是从 1 重来。"""
    legacy = tmp_path / "transcripts.db"
    _make_legacy_db(legacy, [
        ("web:u1", "web", "user", "老问题一", 1000.0, ""),
        ("web:u1", "web", "assistant", "老答案一", 1001.0, ""),
        ("web:u1", "web", "user", "老问题二", 1002.0, ""),
        ("web:u1", "web", "assistant", "老答案二", 1003.0, ""),
    ])
    db = tmp_path / "csagent.db"
    ensure_schema(db)
    migrate_from_transcripts(legacy, db)

    # at 紧跟最后一条历史，确保落回同一段而不是因静默太久开新段
    assert SqliteMemoryStore(db).begin_turn("web:u1", at=1004.0).turn_id == 3


# ---------------------------------------------------------------- 老库补列（M2.2）


def _make_pre_m22_db(path) -> None:
    """造一个 M2.2 之前的 csagent.db：episodes 表没有 next_turn 列。"""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE episodes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
            " seq INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',"
            " started_at REAL NOT NULL, last_active REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, episode_id TEXT NOT NULL, turn_id INTEGER NOT NULL,"
            " channel TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, content TEXT NOT NULL,"
            " display_content TEXT NOT NULL DEFAULT '', attachments TEXT NOT NULL DEFAULT '',"
            " tokens INTEGER NOT NULL DEFAULT 0, interrupted INTEGER NOT NULL DEFAULT 0,"
            " created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO episodes (id, session_id, seq, started_at, last_active)"
            " VALUES ('web:u1#0', 'web:u1', 0, 1000.0, 1003.0)"
        )
        conn.executemany(
            "INSERT INTO messages (session_id, episode_id, turn_id, role, content, created_at)"
            " VALUES ('web:u1', 'web:u1#0', ?, ?, ?, ?)",
            [(1, "user", "q1", 1000.0), (1, "assistant", "a1", 1001.0),
             (2, "user", "q2", 1002.0), (2, "assistant", "a2", 1003.0)],
        )


def test_upgrade_adds_the_counter_column_and_backfills_it(tmp_path):
    """升级后号源要接着历史走，否则新问题会和几个月前的行撞号、插错位置。"""
    db = tmp_path / "csagent.db"
    _make_pre_m22_db(db)

    assert SqliteMemoryStore(db).begin_turn("web:u1", at=1004.0).turn_id == 3


def test_upgrade_keeps_existing_history_readable(tmp_path):
    db = tmp_path / "csagent.db"
    _make_pre_m22_db(db)

    assert [m.content for m in SqliteMemoryStore(db).load("web:u1")] == [
        "q1", "a1", "q2", "a2",
    ]
