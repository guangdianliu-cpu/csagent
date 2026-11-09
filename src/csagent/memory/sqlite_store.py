"""统一持久化记忆层：SQLite 权威存储 + 进程内读缓存（M1）。

一个 store 同时满足三份既有协议，调用方签名零改动：

- ``ShortTermMemoryStore``：``load`` / ``append``，喂模型的富文本
- ``TranscriptStore``：``load_recent`` / ``load_recent_with_files`` /
  ``purge_older_than``，给界面的人话
- ``SessionSnapshotStoreProtocol``：``append_tool_calls`` / ``load_tool_calls`` / ``clear``

**为什么合并**：原先模型记忆在进程内 ``OrderedDict``、界面记录在
``transcripts.db``，两者各写各的。进程一重启，界面还能翻到三天前的对话，模型却
一问三不知，会对用户断言「本会话没发过文件」。合并成一份权威存储之后，
「屏幕上能翻到的」和「模型 rehydrate 出来的」来自同一批行。

读写路径
--------
- 写：``append_turn`` 一个事务写完 ``messages``，再更新 L1 缓存。
  失败只告警不抛——沿用既有语义，一条记录没落盘不该把用户已拿到的回复变成失败请求。
- 读：先查进程内 L1 缓存；miss 时回填（冷启动后第一次请求自动 rehydrate）。

一张表两种文本的分工见 ``sqlite_schema.py`` 的模块注释。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import structlog

from csagent.memory.protocols import TurnHandle
from csagent.memory.session_snapshot import ToolCallSnapshot
from csagent.memory.short_term import DEFAULT_MAX_MESSAGE_CHARS, clip_message
from csagent.memory.sqlite_schema import (
    DEFAULT_EPISODE_GAP_SECONDS,
    connect,
    ensure_schema,
    make_episode_id,
)
from csagent.react.models import ChatMessage

logger = structlog.get_logger(__name__)

DEFAULT_MAX_RETURN = 100
# 结果摘要截断上限，避免大结果把流水撑爆（与进程内后端一致）
_TOOL_PREVIEW_LIMIT = 500


@dataclass
class _WindowCache:
    """某个会话当前 episode 的读缓存。换段后整份作废，避免把上一段原文带进新窗口。"""

    episode_id: str
    items: list[tuple[float, ChatMessage]]


@dataclass(frozen=True)
class TurnSummaryRow:
    """一条 L1 轮次摘要。"""

    episode_id: str
    turn_id: int
    session_id: str
    summary: str
    tokens: int
    created_at: float


@dataclass(frozen=True)
class CheckpointRow:
    """一份 L2 episode Checkpoint。"""

    episode_id: str
    seq: int
    covers_from: int
    covers_to: int
    summary: str
    source_digest: str
    updated_at: float


def _dump_files(files: list[dict] | None) -> str:
    """附件列表 → JSON 字符串。写失败宁可丢附件也不能连累正文入库。"""
    if not files:
        return ""
    try:
        return json.dumps(files, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("memory_files_dump_failed", error=str(exc))
        return ""


def _load_files(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


class SqliteMemoryStore:
    """SQLite 权威存储 + 进程内 LRU 读缓存。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_messages: int = 20,
        max_sessions: int = 2000,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
        max_return: int = DEFAULT_MAX_RETURN,
        max_tool_call_entries: int = 50,
        episode_gap_seconds: float = DEFAULT_EPISODE_GAP_SECONDS,
    ) -> None:
        self._path = Path(db_path)
        self._max_messages = max(2, max_messages)
        self._max_sessions = max(1, max_sessions)
        self._max_message_chars = max_message_chars
        self._max_return = max(1, max_return)
        self._max_tool_call_entries = max(1, max_tool_call_entries)
        self._episode_gap = episode_gap_seconds
        # L1 读缓存：session_id → 当前 episode 的最近 max_messages 条。
        # 权威仍在库里，这里只是省掉每轮一次 SELECT；进程重启后由 load() 自动回填。
        #
        # 为什么连到达时刻一起缓存：并行的两轮会**乱序落库**（先问的可能后写完），
        # 缓存若只按追加顺序排，就会和库里按 created_at 排出来的结果不一致——
        # 那是最难查的一类 bug：重启前后同一个会话的历史顺序不一样。
        #
        # M3.4：缓存按 episode 隔离。隔夜开了新段之后，窗口必须是空的（更早的内容
        # 走 FTS5 召回），不能把上一段原文继续塞进来。
        self._cache: OrderedDict[str, _WindowCache] = OrderedDict()
        ensure_schema(self._path)

    @property
    def path(self) -> Path:
        return self._path

    # ================= 写入 =================

    def append_turn(
        self,
        session_id: str,
        *,
        user_text: str,
        assistant_text: str,
        display_user_text: str | None = None,
        display_assistant_text: str | None = None,
        channel: str = "",
        user_files: list[dict] | None = None,
        assistant_files: list[dict] | None = None,
        interrupted: bool = False,
        turn: TurnHandle | None = None,
    ) -> int:
        """写入一轮对话（统一写入口），返回本轮的 ``turn_id``。

        ``user_text`` / ``assistant_text`` 是喂模型的富文本；
        ``display_*`` 是界面口径的人话，留空表示与富文本相同。

        **写失败只记日志不抛**：一条记录没落盘，不该连带把用户已经拿到的回复
        也变成一次失败的请求。读侧本来就有回落。

        :param session_id: 会话标识；为空直接返回
        :param user_text: 用户输入的富文本（模型口径）
        :param assistant_text: 助手回复的富文本（模型口径）
        :param display_user_text: 用户原话（界面口径）
        :param display_assistant_text: 助手回复的界面口径文本
        :param channel: 来源渠道
        :param user_files: 用户本轮上传的附件
        :param assistant_files: 本轮发出的文件
        :param interrupted: 该轮是否因断连未说完（M0.1）
        :param turn: 入口处预留的定序凭据（M2.2）；留空则此刻现拿一个号，
            那样排序依据会退化成「谁先跑完」，仅供不经 Agent 的直接调用
        :returns: 本轮 turn_id；写失败或无内容时返回 0
        """
        if not session_id:
            return 0

        rows_spec = [
            ("user", user_text, display_user_text, user_files),
            ("assistant", assistant_text, display_assistant_text, assistant_files),
        ]
        # 有文件没文字也要留：用户只拖了个文件进来同样是一轮交互
        rows_spec = [r for r in rows_spec if (r[1] or "").strip() or r[3]]
        if not rows_spec:
            return 0

        try:
            with closing(connect(self._path)) as conn, conn:
                if turn is None:
                    turn = self._reserve_turn(conn, session_id, time.time())
                episode_id, turn_id, at = turn.episode_id, turn.turn_id, turn.started_at
                conn.executemany(
                    "INSERT INTO messages"
                    " (session_id, episode_id, turn_id, channel, role, content,"
                    "  display_content, attachments, tokens, interrupted, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [
                        (
                            session_id, episode_id, turn_id, channel, role,
                            clip_message(text or "", self._max_message_chars),
                            # display 与 content 相同时存空串，省一份重复的大文本
                            self._display_value(text, display),
                            _dump_files(files),
                            1 if (interrupted and role == "assistant") else 0,
                            at,
                        )
                        for role, text, display, files in rows_spec
                    ],
                )
        except sqlite3.Error as exc:
            logger.warning(
                "memory_append_failed", session_id=session_id,
                error=str(exc), path=str(self._path),
            )
            return 0

        self._cache_append(session_id, rows_spec, at, episode_id=episode_id)
        return turn_id

    def append(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """``ShortTermMemoryStore`` 协议：只写模型口径，界面口径回落同一份文本。"""
        self.append_turn(
            session_id, user_text=user_text, assistant_text=assistant_text
        )

    def _display_value(self, content: str | None, display: str | None) -> str:
        """界面文本与模型文本相同时存空串，读取侧回落 ``content``。"""
        if display is None or display == (content or ""):
            return ""
        return clip_message(display, self._max_message_chars)

    # ================= episode 分段与定序 =================

    def begin_turn(self, session_id: str, *, at: float | None = None) -> TurnHandle | None:
        """在请求**进入时**预留本轮的 episode 与 turn_id（M2.2）。

        Agent 在收到消息后立刻调用，把返回的凭据一路带到回写。这样排序依据是
        「谁先问」而不是「谁先跑完」——同一个人并发提两个问题时，两者能差出几十秒。

        :param session_id: 会话标识；为空返回 None
        :param at: 到达时刻，默认取当前时间（测试可注入）
        :returns: 定序凭据；会话为空或库不可用时返回 None，调用方回落到写时分配
        """
        if not session_id:
            return None
        try:
            with closing(connect(self._path)) as conn, conn:
                return self._reserve_turn(conn, session_id, time.time() if at is None else at)
        except sqlite3.Error as exc:
            logger.warning(
                "memory_begin_turn_failed", session_id=session_id, error=str(exc)
            )
            return None

    def _reserve_turn(
        self, conn: sqlite3.Connection, session_id: str, at: float
    ) -> TurnHandle:
        """定位本轮该落进哪个 episode，并从该段的号源取一个 turn_id。

        静默超过 ``episode_gap`` 就开新段——这是没有 UI 的时间线分段（M2.1）：
        用户侧仍然是"一个人一个会话"，但库里有了天然的召回范围和 Checkpoint 边界。

        取号写成「先 INSERT OR IGNORE 建段（号源置 0），再无条件 UPDATE +1」是刻意的，
        为的是两个并发请求同时判定要开新段时也不会撞号：先到的那个真的插入，后到的
        那个 INSERT 被 IGNORE 掉，但两个 UPDATE 都会在写锁下各自把号源推进一格。
        若改成「插入时直接置 1」，被 IGNORE 的那个就会读到和前者相同的号。

        事务必须是 IMMEDIATE（见 ``sqlite_schema.connect``）。DEFERRED 下先 SELECT
        再写，后到的请求会撞 ``SQLITE_BUSY_SNAPSHOT``，本函数的调用方会把整次
        取号当成失败、退回写时分配。

        :param conn: 已开启 IMMEDIATE 事务的连接（BEGIN 时已拿写锁，自增才是原子的）
        :param session_id: 会话标识
        :param at: 到达时刻，同时用于判断是否跨段
        :returns: 本轮的定序凭据
        """
        row = conn.execute(
            "SELECT id, seq, last_active FROM episodes"
            " WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        if row is not None and at - float(row[2] or 0.0) <= self._episode_gap:
            episode_id, seq = str(row[0]), int(row[1])
        else:
            seq = 0 if row is None else int(row[1]) + 1
            episode_id = make_episode_id(session_id, seq)

        conn.execute(
            "INSERT OR IGNORE INTO episodes"
            " (id, session_id, seq, title, started_at, last_active, next_turn)"
            " VALUES (?, ?, ?, '', ?, ?, 0)",
            (episode_id, session_id, seq, at, at),
        )
        conn.execute(
            "UPDATE episodes SET last_active = ?, next_turn = next_turn + 1 WHERE id = ?",
            (at, episode_id),
        )
        turn_row = conn.execute(
            "SELECT next_turn FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return TurnHandle(
            session_id=session_id,
            episode_id=episode_id,
            turn_id=int(turn_row[0]),
            started_at=at,
        )

    def current_episode_id(self, session_id: str) -> str:
        """当前（最近）episode 的 id；该会话还没有任何记录时返回空串。"""
        try:
            with closing(connect(self._path)) as conn:
                row = conn.execute(
                    "SELECT id FROM episodes WHERE session_id = ?"
                    " ORDER BY seq DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row else ""

    # ================= 读取：模型口径 =================

    def load(self, session_id: str) -> list[ChatMessage]:
        """``ShortTermMemoryStore`` 协议：当前 episode 最近 ``max_messages`` 条富文本。

        M3.4 起默认只注入**当前段**。更早的内容靠 FTS5 召回，不再无差别塞进窗口——
        否则隔了三个月的对话会继续占预算，Checkpoint 也概括不完。

        L1 缓存 miss 或换段后从库里回填。
        """
        if not session_id:
            return []
        episode_id = self.current_episode_id(session_id)
        cached = self._cache.get(session_id)
        if cached is None or cached.episode_id != episode_id:
            items = self._read_window(session_id, episode_id)
            self._cache_put(session_id, episode_id, items)
        else:
            self._cache.move_to_end(session_id)
            items = cached.items
        return [message for _at, message in items]

    def _read_window(
        self, session_id: str, episode_id: str
    ) -> list[tuple[float, ChatMessage]]:
        """从库里读当前 episode 最近 ``max_messages`` 条，按**到达**时间正序返回。

        排序键是 ``created_at``（请求到达时刻）而非 ``id``（落库顺序）：并行提问
        时两者会分叉，见 ``sqlite_schema`` 里 idx_messages_session_time 的注释。

        :returns: (到达时刻, 消息) 列表；到达时刻供缓存维持同一套排序
        """
        if not episode_id:
            return []
        try:
            with closing(connect(self._path)) as conn:
                rows = conn.execute(
                    "SELECT role, content, created_at FROM messages"
                    " WHERE session_id = ? AND episode_id = ?"
                    " ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, episode_id, self._max_messages),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("memory_load_failed", session_id=session_id, error=str(exc))
            return []
        return [
            (float(at or 0.0), ChatMessage(role=r, content=c))
            for r, c, at in reversed(rows)
        ]

    # ================= 读取：界面口径 =================

    def load_recent(self, session_id: str, limit: int | None = None) -> list[ChatMessage]:
        """``TranscriptStore`` 协议：最近若干条，按时间正序（最早的在前）。"""
        return [
            ChatMessage(role=item["role"], content=item["content"])
            for item in self.load_recent_with_files(session_id, limit)
        ]

    def load_recent_with_files(
        self, session_id: str, limit: int | None = None
    ) -> list[dict]:
        """给 ``/api/chat/history`` 用：连附件一起读回，返回界面口径文本。

        与 ``load`` 分开：那个返回喂给模型的富文本，塞进界面会把几千字的附件块
        糊满聊天区；这里返回的 ``content`` 是 ``display_content``（空则回落）。
        """
        if not session_id:
            return []
        count = self._max_return if limit is None else max(1, min(limit, self._max_return))
        try:
            with closing(connect(self._path)) as conn:
                rows = conn.execute(
                    "SELECT role, content, display_content, attachments FROM messages"
                    " WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (session_id, count),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("memory_load_failed", session_id=session_id, error=str(exc))
            return []
        return [
            {"role": role, "content": display or content, "files": _load_files(raw)}
            for role, content, display, raw in reversed(rows)
        ]

    def purge_older_than(self, days: float) -> int:
        """``TranscriptStore`` 协议：删掉 ``days`` 天前的记录，返回删除条数。

        ``days <= 0`` 表示不清理，**这是默认值**：评审决定默认永不删除，
        膨胀由召回窗口而非存储上限来治理（见设计文档 M2.3）。保留本方法是为了
        协议兼容与将来按需启用。
        """
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        try:
            with closing(connect(self._path)) as conn, conn:
                cursor = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
                removed = cursor.rowcount or 0
        except sqlite3.Error as exc:
            logger.warning("memory_purge_failed", error=str(exc))
            return 0
        self._cache.clear()  # 删过之后缓存可能领先于库，整体作废最省事
        return removed

    # ================= 工具流水 =================

    def append_tool_calls(
        self,
        session_id: str,
        calls: list[ToolCallSnapshot],
        *,
        turn: TurnHandle | None = None,
    ) -> None:
        """追加本轮工具调用流水。

        带上 ``turn`` 时直接按凭据归属（M2.2）。这一步非做不可：调用点
        （``_append_tool_call_snapshots``）排在 ``_write_back_memory`` **之前**
        ——Checkpoint 刷新要读到本轮工具流水才能写出「已完成操作」——所以此刻本轮的
        messages 还没落库，从库里"取最新一轮"只会取到**上一轮**。

        :param session_id: 会话标识
        :param calls: 本轮成功的工具调用快照
        :param turn: 入口处预留的定序凭据；留空则回落到"最新一轮"的旧口径
        """
        if not session_id or not calls:
            return
        try:
            with closing(connect(self._path)) as conn, conn:
                if turn is not None:
                    episode_id, turn_id = turn.episode_id, turn.turn_id
                else:
                    row = conn.execute(
                        "SELECT episode_id, turn_id FROM messages"
                        " WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    episode_id = str(row[0]) if row else make_episode_id(session_id, 0)
                    turn_id = int(row[1]) if row else 0
                conn.executemany(
                    "INSERT INTO tool_calls"
                    " (session_id, episode_id, turn_id, tool_name, args_preview,"
                    "  result_preview, ok, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    [
                        (
                            session_id, episode_id, turn_id,
                            c.tool_name,
                            (c.arguments_summary or "")[:_TOOL_PREVIEW_LIMIT],
                            (c.result_summary or "")[:_TOOL_PREVIEW_LIMIT],
                            c.timestamp,
                        )
                        for c in calls
                    ],
                )
        except sqlite3.Error as exc:
            logger.warning(
                "memory_tool_calls_append_failed", session_id=session_id, error=str(exc)
            )

    def load_tool_calls(self, session_id: str, last_n: int = 10) -> list[ToolCallSnapshot]:
        """加载最近 N 次工具调用（按时间正序返回）。"""
        if not session_id:
            return []
        limit = last_n if last_n > 0 else self._max_tool_call_entries
        try:
            with closing(connect(self._path)) as conn:
                rows = conn.execute(
                    "SELECT tool_name, args_preview, result_preview, created_at"
                    " FROM tool_calls WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "memory_tool_calls_load_failed", session_id=session_id, error=str(exc)
            )
            return []
        return [
            ToolCallSnapshot(
                tool_name=name,
                arguments_summary=args or "",
                result_summary=result or "",
                timestamp=float(at or 0.0),
            )
            for name, args, result, at in reversed(rows)
        ]

    def clear(self, session_id: str) -> None:
        """清除该会话的工具流水与读缓存（对话本身保留）。"""
        self._cache.pop(session_id, None)
        try:
            with closing(connect(self._path)) as conn, conn:
                conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
        except sqlite3.Error as exc:
            logger.warning("memory_clear_failed", session_id=session_id, error=str(exc))

    # ================= L1 缓存 =================

    def _cache_put(
        self,
        session_id: str,
        episode_id: str,
        history: list[tuple[float, ChatMessage]],
    ) -> None:
        self._cache[session_id] = _WindowCache(episode_id=episode_id, items=list(history))
        self._cache.move_to_end(session_id)
        while len(self._cache) > self._max_sessions:
            self._cache.popitem(last=False)

    def _cache_append(
        self,
        session_id: str,
        rows_spec: list[tuple],
        at: float,
        *,
        episode_id: str,
    ) -> None:
        """把刚落库的这一轮补进缓存；缓存里还没有该会话时不预热（留给下次 load）。

        ``at`` 早于缓存末尾时要重排：并行的两轮按「谁先跑完」落库，但按「谁先问」
        排序，所以后写入的那轮可能要插到中间去。不重排的话，同一个会话在重启前
        （读缓存）和重启后（读库）会看到两种顺序。

        换段时整份缓存作废：新段的窗口应从空开始，上一段走召回而不是原文续灌。

        :param session_id: 会话标识
        :param rows_spec: 本轮待写的行，结构同 ``append_turn``
        :param at: 本轮的到达时刻，即这些行的 ``created_at``
        :param episode_id: 本轮所属 episode
        """
        cached = self._cache.get(session_id)
        if cached is None:
            return
        if cached.episode_id != episode_id:
            self._cache.pop(session_id, None)
            return
        items = cached.items
        out_of_order = bool(items) and at < items[-1][0]
        for role, text, _display, _files in rows_spec:
            items.append(
                (
                    at,
                    ChatMessage(
                        role=role,
                        content=clip_message(text or "", self._max_message_chars),
                    ),
                )
            )
        if out_of_order:
            # 按到达时刻稳定排序：同一轮的 user/assistant 时刻相同，
            # 稳定性保证它们不会被调换成 assistant 在前。
            items.sort(key=lambda item: item[0])
        if len(items) > self._max_messages:
            del items[: len(items) - self._max_messages]
        self._cache.move_to_end(session_id)

    # ================= 分级记忆（M3.3）=================

    def upsert_turn_summary(
        self,
        *,
        episode_id: str,
        turn_id: int,
        session_id: str,
        summary: str,
        tokens: int,
        created_at: float | None = None,
    ) -> None:
        """写入或覆盖一条 L1 轮次摘要。失败只告警。"""
        if not episode_id or turn_id <= 0 or not (summary or "").strip():
            return
        at = time.time() if created_at is None else created_at
        try:
            with closing(connect(self._path)) as conn, conn:
                conn.execute(
                    "INSERT INTO turn_summaries"
                    " (episode_id, turn_id, session_id, summary, tokens, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(episode_id, turn_id) DO UPDATE SET"
                    " summary = excluded.summary, tokens = excluded.tokens",
                    (episode_id, turn_id, session_id, summary.strip(), int(tokens), at),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "turn_summary_write_failed",
                episode_id=episode_id, turn_id=turn_id, error=str(exc),
            )

    def list_turn_summaries(self, episode_id: str) -> list[TurnSummaryRow]:
        """当前 episode 的全部 L1，按 turn_id 正序。"""
        if not episode_id:
            return []
        try:
            with closing(connect(self._path)) as conn:
                rows = conn.execute(
                    "SELECT episode_id, turn_id, session_id, summary, tokens, created_at"
                    " FROM turn_summaries WHERE episode_id = ? ORDER BY turn_id",
                    (episode_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("turn_summary_load_failed", episode_id=episode_id, error=str(exc))
            return []
        return [
            TurnSummaryRow(
                episode_id=str(ep),
                turn_id=int(tid),
                session_id=str(sid or ""),
                summary=str(summary or ""),
                tokens=int(tokens or 0),
                created_at=float(at or 0.0),
            )
            for ep, tid, sid, summary, tokens, at in rows
        ]

    def upsert_checkpoint(
        self,
        *,
        episode_id: str,
        seq: int,
        covers_from: int,
        covers_to: int,
        summary: str,
        source_digest: str = "",
    ) -> None:
        """写入或覆盖一份 L2 Checkpoint。失败只告警。"""
        if not episode_id or not (summary or "").strip():
            return
        try:
            with closing(connect(self._path)) as conn, conn:
                conn.execute(
                    "INSERT INTO checkpoints"
                    " (episode_id, seq, covers_from, covers_to, summary,"
                    "  source_digest, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(episode_id, seq) DO UPDATE SET"
                    " covers_from = excluded.covers_from,"
                    " covers_to = excluded.covers_to,"
                    " summary = excluded.summary,"
                    " source_digest = excluded.source_digest,"
                    " updated_at = excluded.updated_at",
                    (
                        episode_id, int(seq), int(covers_from), int(covers_to),
                        summary.strip(), source_digest, time.time(),
                    ),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "checkpoint_write_failed", episode_id=episode_id, error=str(exc)
            )

    def latest_checkpoint(self, episode_id: str) -> CheckpointRow | None:
        """当前 episode 最新一份 L2；没有则 None。"""
        if not episode_id:
            return None
        try:
            with closing(connect(self._path)) as conn:
                row = conn.execute(
                    "SELECT episode_id, seq, covers_from, covers_to, summary,"
                    " source_digest, updated_at"
                    " FROM checkpoints WHERE episode_id = ? ORDER BY seq DESC LIMIT 1",
                    (episode_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("checkpoint_load_failed", episode_id=episode_id, error=str(exc))
            return None
        if row is None:
            return None
        return CheckpointRow(
            episode_id=str(row[0]),
            seq=int(row[1]),
            covers_from=int(row[2] or 0),
            covers_to=int(row[3] or 0),
            summary=str(row[4] or ""),
            source_digest=str(row[5] or ""),
            updated_at=float(row[6] or 0.0),
        )
