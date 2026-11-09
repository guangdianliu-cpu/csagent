"""对话记录持久化（SQLite）：给界面翻历史用，不进模型上下文。

和 ``short_term`` 的分工必须说清楚，否则很容易被当成「模型记忆变长了」：

===============  ==========================  ==============================
                 ``short_term``               本模块
===============  ==========================  ==============================
是什么           模型的记忆窗口               给人看的对话记录
进不进提示词     进                          **不进**
容量             ``max_history_messages``    每次取最近 ``max_return`` 条
                 = 20 条封顶                  （默认 100），按天清理
活多久           进程内后端重启即清空         落盘，``retention_days`` 前的删掉
===============  ==========================  ==============================

**刻意不合并成一个**：窗口一放大，每轮的压缩成本和 Token 都跟着涨；而界面要翻的
是几十轮以前的原话，那部分模型本来就只剩 Checkpoint 摘要。拆开之后「能翻多久」
和「记得多少」是两个独立旋钮，调一个不会牵动另一个。

由此有一条必须接受的事实：**屏幕上能翻到的，不等于模型还记得的。** 这不是缺陷，
是所有对话产品的常态（窗口外靠 Checkpoint 摘要兜底，不是完全失忆）。

为什么是 SQLite 而不是继续用 JSON/JSONL：要按 ``session_id`` 取最近 N 条、要按时间
批量清理，这两件事在按天分片的 JSONL 上都得全量扫。SQLite 是标准库自带，不引入部署
依赖，两条索引就够用。

并发：每次操作单开连接（连接建立在已缓存的库上是微秒级），因此天然跨线程安全，
不需要共享连接和锁。WAL 模式下读不阻塞写。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import structlog

from csagent.memory.short_term import DEFAULT_MAX_MESSAGE_CHARS, clip_message
from csagent.react.models import ChatMessage

logger = structlog.get_logger(__name__)

DEFAULT_MAX_RETURN = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    channel     TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    -- 本条消息涉及的文件，JSON 数组 [{"filename":..,"workspace_path":..}]。
    -- 只存路径不存下载链接：链接里的 dl 授权 24 小时就过期，而记录要留 30 天，
    -- 存下来的必然是死链。读取时（/api/chat/history）按需现签一个新的。
    attachments TEXT    NOT NULL DEFAULT ''
);
-- 取某会话最近 N 条：按 (session_id, id) 倒序扫，命中索引即停
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
-- 启动时按时间批量清理
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


def _dump_files(files: list[dict] | None) -> str:
    """附件列表 → JSON 字符串。写失败宁可丢附件也不能连累正文入库。"""
    if not files:
        return ""
    try:
        return json.dumps(files, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("transcript_files_dump_failed", error=str(exc))
        return ""


def _load_files(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


class SqliteTranscriptStore:
    """把每轮对话追加到 SQLite，供 ``GET /api/chat/history`` 回填聊天区。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
        max_return: int = DEFAULT_MAX_RETURN,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_message_chars = max_message_chars
        self._max_return = max(1, max_return)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给 attachments 列之前建的库补列。

        SQLite 的 ADD COLUMN 是纯元数据操作（不重写数据文件），几毫秒完事，
        放在启动路径上没有负担。重复执行会报 duplicate column，吞掉即可——
        没有 IF NOT EXISTS 可用，探测再加反而多一次查询。
        """
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT ''")
            conn.commit()
            logger.info("transcript_migrated", column="attachments")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        # WAL：读写互不阻塞，企微长连接与 HTTP 请求可能同时落库。
        # synchronous=NORMAL：WAL 下不会丢已提交事务，只在断电时可能丢最后几条——
        # 对话记录不是账目，用一次 fsync 换每轮的写延迟不划算。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------ 写入

    def append(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        channel: str = "",
        user_files: list[dict] | None = None,
        assistant_files: list[dict] | None = None,
    ) -> None:
        """追加一轮对话。

        **写失败只记日志不抛**：一条记录没落盘，不该连带把用户已经拿到的回复
        也变成一次失败的请求。读侧（``Agent.load_history``）本来就有回落。
        """
        if not session_id:
            return
        now = time.time()
        rows = [
            (
                session_id,
                channel,
                role,
                clip_message(text, self._max_message_chars),
                now,
                _dump_files(files),
            )
            for role, text, files in (
                ("user", user_text, user_files),
                ("assistant", assistant_text, assistant_files),
            )
            # 有文件没文字也要留：用户只拖了个文件进来同样是一轮交互
            if (text or "").strip() or files
        ]
        if not rows:
            return
        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany(
                    "INSERT INTO messages"
                    " (session_id, channel, role, content, created_at, attachments)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            logger.warning(
                "transcript_append_failed",
                session_id=session_id,
                error=str(exc),
                path=str(self._path),
            )

    # ------------------------------------------------------------------ 读取

    def load_recent(self, session_id: str, limit: int | None = None) -> list[ChatMessage]:
        """取某会话最近若干条，按时间正序返回（最早的在前，直接铺进聊天区）。"""
        if not session_id:
            return []
        count = self._max_return if limit is None else max(1, min(limit, self._max_return))
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (session_id, count),
            )
            rows = cursor.fetchall()
        # 倒序取「最近 N 条」，再翻回正序给前端
        return [ChatMessage(role=role, content=content) for role, content in reversed(rows)]

    # ------------------------------------------------------------------ 清理

    def load_recent_with_files(
        self, session_id: str, limit: int | None = None
    ) -> list[dict]:
        """给 /api/chat/history 用：连附件一起读回。

        与 ``load_recent`` 分开：那个返回 ``ChatMessage``，是喂给模型的口径，
        塞附件进去会顺着提示词一路漏下去。这里返回裸 dict，只服务界面。
        """
        if not session_id:
            return []
        n = max(1, limit or self._max_return)
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT role, content, attachments FROM messages"
                    " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, n),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "transcript_load_failed", session_id=session_id, error=str(exc)
            )
            return []
        out = []
        for role, content, raw in reversed(rows):
            out.append(
                {"role": role, "content": content, "files": _load_files(raw)}
            )
        return out

    def purge_older_than(self, days: float) -> int:
        """删掉 ``days`` 天前的记录，返回删除条数。``days <= 0`` 表示不清理。

        不做 VACUUM：删出来的页 SQLite 会自己复用，而 VACUUM 要重写整个库文件、
        期间持写锁。库无限膨胀的风险已经由这条定期删除挡住了。
        """
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            return cursor.rowcount or 0
