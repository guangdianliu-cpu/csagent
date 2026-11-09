"""统一持久化记忆层的建表、连接参数与历史库迁移（M1）。

与 ``transcript.py`` 的关系
--------------------------
``transcript.py`` 的 ``transcripts.db`` 只服务界面翻历史；模型记忆在进程内
``OrderedDict`` 里，重启即失忆。本模块把两者合并成**唯一权威存储**
``data/csagent.db``：同一张 ``messages`` 表既回填界面、也 rehydrate 模型记忆。

一张表两种文本
--------------
每轮对话实际上有两份**内容不同**的文本，不能压成一列：

- ``content``：喂给模型的富文本。附件块、工作区路径、内容预览、默认指令全在里面，
  动辄几千字。模型靠里面的路径继续操作文件，少一个字都可能让下一轮找不到文件。
- ``display_content``：界面上显示的人话。用户当时打的那句话，加一句
  「[附件] xx.xlsx」。为空表示两者相同，读取时回落 ``content``。

这是 ``agent._write_back_memory`` 里既有的分工（原先靠两个 store 分别承载），
合并存储时必须原样保住，否则要么界面被几千字的富文本糊满，要么模型丢掉文件路径。

本期建哪些表
------------
M1/M2：``episodes`` / ``messages`` / ``tool_calls`` / ``artifacts``。
M3.3：``turn_summaries``（L1 轮次摘要）/ ``checkpoints``（L2 episode 快照）。
M3.4：``messages_fts`` / ``turn_summaries_fts`` / ``artifacts_fts``（FTS5，unicode61）。
FTS 表的触发器与回填见 ``ensure_fts``——老库升级时会 rebuild 一次。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# 静默多久算一个新 episode（秒）。12 小时是「睡一觉再来」与「接着刚才聊」的分界。
DEFAULT_EPISODE_GAP_SECONDS = 12 * 3600


# ========== 建表 ==========

SCHEMA = """
-- ---------- 时间线分段：解决记录膨胀，无 UI（M2.1）----------
-- session_id 仍是身份（web:{uid} / wecom:{...}），一个身份一条无限时间线；
-- episode 在这条时间线上按静默间隔切段，给 Checkpoint 和召回一个天然范围。
CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT    PRIMARY KEY,
    session_id   TEXT    NOT NULL,
    seq          INTEGER NOT NULL,      -- 该身份下的第几段，从 0 开始
    title        TEXT    NOT NULL DEFAULT '',
    started_at   REAL    NOT NULL,
    last_active  REAL    NOT NULL,
    -- 已分配出去的 turn_id 水位（M2.2）。并行提问时两个请求都要在**到达时**拿号，
    -- 那一刻两轮的 messages 都还没落库，从 MAX(turn_id) 推只会让两者拿到同一个号。
    -- 所以号源必须是一个可以在单事务里自增的计数器，而不是从已有行推算。
    next_turn    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id, seq DESC);

-- ---------- 对话轮次：唯一权威，同时服务界面与模型 ----------
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    episode_id      TEXT    NOT NULL,
    turn_id         INTEGER NOT NULL,   -- episode 内递增，并发定序用（M2.2）
    channel         TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,   -- 给模型的富文本
    display_content TEXT    NOT NULL DEFAULT '',  -- 给界面的人话；空表示同 content
    -- 本条消息涉及的文件，JSON 数组 [{"filename":..,"workspace_path":..}]。
    -- 只存路径不存下载链接：链接里的 dl 授权 24 小时就过期，而记录永久保留，
    -- 存下来的必然是死链。读取时（/api/chat/history）按需现签一个新的。
    attachments     TEXT    NOT NULL DEFAULT '',
    tokens          INTEGER NOT NULL DEFAULT 0,   -- 预先算好，省去每次装配重算
    interrupted     INTEGER NOT NULL DEFAULT 0,   -- M0.1：该轮是否被中断
    created_at      REAL    NOT NULL
);
-- 取某会话最近 N 条：按 (session_id, created_at) 倒序扫，命中索引即停。
-- 排序键是 created_at 而非 id：id 是**落库**顺序，并行提问时先问后答的那轮会排到
-- 后面去；created_at 存的是请求到达时刻（见 protocols.TurnHandle），才是用户眼里
-- 的顺序。同刻到达时用 id 兜底，保证结果稳定。
--
-- NOTE: 老的 idx_messages_session(session_id, id) 仍然留着，没有改名重建。
-- CREATE INDEX IF NOT EXISTS 碰到同名索引会直接跳过、不会按新定义重建，想换定义
-- 就得每次启动先 DROP——在十万行量级的表上白白重建一遍索引，不值当。
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_session_time
    ON messages(session_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_episode ON messages(episode_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

-- ---------- 工具流水（替代进程内 _tool_calls）----------
CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT    NOT NULL,
    episode_id     TEXT    NOT NULL,
    turn_id        INTEGER NOT NULL,
    tool_name      TEXT    NOT NULL,
    args_preview   TEXT    NOT NULL DEFAULT '',
    result_preview TEXT    NOT NULL DEFAULT '',   -- 500 字符，同现状
    artifact_id    TEXT,                          -- 大结果外置引用（M3.1）
    ok             INTEGER NOT NULL DEFAULT 1,
    duration_ms    INTEGER,
    created_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, id);

-- ---------- 产物登记（M2.4）----------
-- 文件默认永不删除，expires_at 只给 tool_result 这类可能高频产生的类别留口子。
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    episode_id  TEXT    NOT NULL DEFAULT '',
    turn_id     INTEGER,
    kind        TEXT    NOT NULL,       -- inbound / outbound / script / tool_result
    path        TEXT    NOT NULL,
    filename    TEXT    NOT NULL,
    sha256      TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    expires_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_filename ON artifacts(filename);

-- ---------- 分级记忆（M3.3）----------
-- L1：每轮一条，约 200 token。作用域是 episode，不跨段。
CREATE TABLE IF NOT EXISTS turn_summaries (
    episode_id TEXT    NOT NULL,
    turn_id    INTEGER NOT NULL,
    session_id TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL,
    tokens     INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    PRIMARY KEY (episode_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_summaries_session
    ON turn_summaries(session_id, created_at);

-- L2：每 N 条 L1 合并成一份 episode Checkpoint。seq 在段内递增。
CREATE TABLE IF NOT EXISTS checkpoints (
    episode_id    TEXT    NOT NULL,
    seq           INTEGER NOT NULL,
    covers_from   INTEGER,
    covers_to     INTEGER,
    summary       TEXT    NOT NULL,
    source_digest TEXT    NOT NULL DEFAULT '',
    updated_at    REAL    NOT NULL,
    PRIMARY KEY (episode_id, seq)
);
"""


# ========== 连接 ==========

def connect(db_path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """打开一个连接并设好并发相关的 PRAGMA。

    每次操作单开连接（连接建立在已缓存的库上是微秒级），因此天然跨线程安全，
    不需要共享连接和锁——沿用 ``transcript.py`` 已验证的做法。

    三个 PRAGMA 外加写事务隔离级别，是单机多 worker 可行性的前提：

    - ``journal_mode=WAL``：读不阻塞写。企微长连接与 HTTP 请求会同时落库。
      这是**持久化**属性，只需设一次，但重复设无害。
    - ``synchronous=NORMAL``：WAL 下不会丢已提交事务，只在断电时可能丢最后几条。
      对话记录不是账目，用一次 fsync 换每轮的写延迟不划算。
    - ``busy_timeout``：拿不到锁时最多等这么久再报 database is locked，
      而不是立刻失败。多 worker 下这条决定了并发写是"慢一点"还是"报错"。
    - ``isolation_level=IMMEDIATE``：``with conn:`` 发出 ``BEGIN IMMEDIATE``，
      一进事务就拿写锁。默认 DEFERRED 会先 SELECT 拍快照再写，WAL 下另一连接
      已提交时升级写锁得到 ``SQLITE_BUSY_SNAPSHOT``，busy_timeout **不重试**
      这种错误。``begin_turn`` 抓住后返回 None，排序退回完成序——M2.2 在
      企微连发 / 多 worker 下会静默失效。

    :param db_path: 库文件路径
    :param timeout: Python 侧的连接超时（秒）
    :returns: 已配置好的连接，调用方负责关闭
    """
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    # PRAGMA 跑完再改隔离级别，避免 journal_mode 自己进 IMMEDIATE 事务。
    conn.isolation_level = "IMMEDIATE"
    return conn


# 建表语句之后补加的列：(表, 列, 类型与默认值)。
# CREATE TABLE IF NOT EXISTS 对已存在的表是整条跳过的，不会比对列——
# 新列只能靠 ALTER 补，否则升级后第一次写就是 "no such column"。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("episodes", "next_turn", "INTEGER NOT NULL DEFAULT 0"),
)


def ensure_schema(db_path: str | Path) -> None:
    """建库建表（可重入），并把老库缺的列补齐。

    :param db_path: 库文件路径；父目录不存在时自动创建
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as conn, conn:
        conn.executescript(SCHEMA)
        if _add_missing_columns(conn):
            _backfill_next_turn(conn)
        from csagent.memory.sqlite_fts import ensure_fts

        ensure_fts(conn)


def _add_missing_columns(conn: sqlite3.Connection) -> bool:
    """按 ``_ADDED_COLUMNS`` 给老库补列，返回是否真的补过。

    :param conn: 已开启事务的连接
    :returns: 有列被新增时为 True，调用方据此决定要不要回填
    """
    added = False
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        added = True
    return added


def _backfill_next_turn(conn: sqlite3.Connection) -> None:
    """把 ``episodes.next_turn`` 拉到各段已有的 turn_id 水位。

    不回填的话，升级后老 episode 的号源是 0，下一轮拿到 1，直接和历史行撞号——
    翻历史会看到新问题插在几个月前的位置上。

    :param conn: 已开启事务的连接
    """
    conn.execute(
        "UPDATE episodes SET next_turn = COALESCE("
        "    (SELECT MAX(turn_id) FROM messages WHERE messages.episode_id = episodes.id),"
        "    0)"
    )


# ========== 历史库迁移 ==========

def migrate_from_transcripts(
    legacy_path: str | Path,
    db_path: str | Path,
    *,
    episode_gap_seconds: float = DEFAULT_EPISODE_GAP_SECONDS,
) -> int:
    """把旧 ``transcripts.db`` 的对话搬进新库，返回迁移的消息条数。

    **可重入**：已经迁过的库（``messages`` 非空）直接跳过，不会写重复数据。
    因此可以无条件挂在启动路径上。

    旧库只有界面口径的文本（模型侧的富文本历史从未落过盘，它一直在进程内存里），
    所以 ``content`` 与 ``display_content`` 都填旧值——这是现有数据的全部信息量。

    episode 按旧记录的时间间隔重新切分，而不是全部塞进 episode 0：既然要搬一次，
    顺手把时间线分好，省得历史数据在召回时永远是一整坨。

    :param legacy_path: 旧 transcripts.db 路径；不存在则直接返回 0
    :param db_path: 新库路径，必须已经 ``ensure_schema``
    :param episode_gap_seconds: 静默多久切一个新 episode
    :returns: 迁移的消息条数；跳过或无数据时为 0
    """
    legacy = Path(legacy_path)
    if not legacy.exists():
        return 0

    with closing(connect(db_path)) as conn:
        already = conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
    if already is not None:
        logger.debug("memory_migration_skipped", reason="target_not_empty")
        return 0

    try:
        with closing(connect(legacy)) as src:
            rows = src.execute(
                "SELECT session_id, channel, role, content, created_at, attachments"
                " FROM messages ORDER BY session_id, id"
            ).fetchall()
    except sqlite3.Error as exc:
        # 旧库损坏/表结构对不上，不能因此挡住服务启动——新库照常用，历史丢了记一条告警
        logger.warning("memory_migration_read_failed", path=str(legacy), error=str(exc))
        return 0

    if not rows:
        return 0

    episodes, messages = _replay_legacy_rows(rows, episode_gap_seconds)
    try:
        with closing(connect(db_path)) as conn, conn:
            conn.executemany(
                "INSERT INTO episodes"
                " (id, session_id, seq, title, started_at, last_active, next_turn)"
                " VALUES (?, ?, ?, '', ?, ?, ?)",
                episodes,
            )
            conn.executemany(
                "INSERT INTO messages"
                " (session_id, episode_id, turn_id, channel, role, content,"
                "  display_content, attachments, tokens, interrupted, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                messages,
            )
    except sqlite3.Error as exc:
        logger.warning("memory_migration_write_failed", error=str(exc))
        return 0

    logger.info(
        "memory_migrated_from_transcripts",
        messages=len(messages),
        episodes=len(episodes),
        source=str(legacy),
    )
    return len(messages)


def _replay_legacy_rows(
    rows: list[tuple], episode_gap_seconds: float
) -> tuple[list[tuple], list[tuple]]:
    """把旧库的扁平消息流重放成 (episodes, messages) 两组待插入行。

    旧库没有 episode / turn 概念，这里按 ``session_id`` 分组后顺序扫描：
    相邻两条的时间差超过 ``episode_gap_seconds`` 就开一段新的，turn_id 在段内重新计数。

    每段收尾时把当时的 ``turn_id`` 一并写进 ``episodes.next_turn``，让号源接着历史
    往下走；否则迁移完的第一轮会拿到 1，和搬进来的历史行撞号。

    :param rows: 旧库行 (session_id, channel, role, content, created_at, attachments)
    :param episode_gap_seconds: 切段阈值
    :returns: (episodes 插入行, messages 插入行)
    """
    episodes: list[tuple] = []
    messages: list[tuple] = []

    current_session = None
    episode_id = ""
    episode_seq = -1
    episode_started = 0.0
    turn_id = 0
    prev_at = 0.0

    for session_id, channel, role, content, created_at, attachments in rows:
        created_at = float(created_at or 0.0)
        is_new_session = session_id != current_session
        is_gap = created_at - prev_at > episode_gap_seconds

        if is_new_session or is_gap:
            if episode_id:
                episodes.append(
                    (
                        episode_id, current_session, episode_seq,
                        episode_started, prev_at, turn_id,
                    )
                )
            episode_seq = 0 if is_new_session else episode_seq + 1
            current_session = session_id
            episode_id = make_episode_id(session_id, episode_seq)
            episode_started = created_at
            turn_id = 0

        # 旧库一轮是 user + assistant 两行；user 行开启新 turn
        if role == "user":
            turn_id += 1
        messages.append(
            (
                session_id, episode_id, turn_id, channel or "", role,
                content or "", content or "", attachments or "", created_at,
            )
        )
        prev_at = created_at

    if episode_id:
        episodes.append(
            (episode_id, current_session, episode_seq, episode_started, prev_at, turn_id)
        )
    return episodes, messages


def make_episode_id(session_id: str, seq: int) -> str:
    """episode 主键：``{session_id}#{seq}``。

    用确定性拼接而不是 UUID，是为了迁移与重放可重入——同一条时间线重放两次
    得到同一批 id，插入冲突即可发现，不会悄悄产生两套并行的 episode。
    """
    return f"{session_id}#{seq}"


def now() -> float:
    """统一时间源，便于测试替换。"""
    return time.time()
