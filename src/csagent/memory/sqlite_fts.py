"""FTS5 倒排索引：跨 episode 召回的存储侧（M3.4）。

三张虚表分别挂在 ``messages.content``、``turn_summaries.summary``、
``artifacts.filename`` 上，用 ``content=`` 外部内容模式——正文仍只存一份，
FTS 只持有倒排。分词选 ``unicode61``：零扩展依赖，中文按字、标识按整词，
BM25 对「产品代码 / 文件名」这类短标识足够用。

embedding 不做。效果不够时再加独立文件，不要在这里混进向量库。
"""

from __future__ import annotations

import sqlite3

import structlog

logger = structlog.get_logger(__name__)

# 外部内容 + 触发器：INSERT/UPDATE/DELETE 正文时同步倒排。
# CREATE TRIGGER IF NOT EXISTS 可重入；老库第一次跑会建触发器并 rebuild。
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS turn_summaries_fts USING fts5(
    summary,
    content='turn_summaries',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS turn_summaries_fts_ai AFTER INSERT ON turn_summaries BEGIN
    INSERT INTO turn_summaries_fts(rowid, summary) VALUES (new.rowid, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS turn_summaries_fts_ad AFTER DELETE ON turn_summaries BEGIN
    INSERT INTO turn_summaries_fts(turn_summaries_fts, rowid, summary)
        VALUES('delete', old.rowid, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS turn_summaries_fts_au AFTER UPDATE ON turn_summaries BEGIN
    INSERT INTO turn_summaries_fts(turn_summaries_fts, rowid, summary)
        VALUES('delete', old.rowid, old.summary);
    INSERT INTO turn_summaries_fts(rowid, summary) VALUES (new.rowid, new.summary);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    filename,
    content='artifacts',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS artifacts_fts_ai AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts(rowid, filename) VALUES (new.rowid, new.filename);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_fts_ad AFTER DELETE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, filename)
        VALUES('delete', old.rowid, old.filename);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_fts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, filename)
        VALUES('delete', old.rowid, old.filename);
    INSERT INTO artifacts_fts(rowid, filename) VALUES (new.rowid, new.filename);
END;
"""


def fts5_available(conn: sqlite3.Connection) -> bool:
    """当前 SQLite 是否编进了 FTS5。未编进时召回静默关闭，不挡服务启动。"""
    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
    except sqlite3.Error:
        return False
    return any("FTS5" in str(row[0]).upper() for row in rows)


def ensure_fts(conn: sqlite3.Connection) -> None:
    """建 FTS 虚表与触发器；老库正文已在、倒排还空时 rebuild 一次。

    :param conn: 已开启事务的连接
    """
    if not fts5_available(conn):
        logger.warning("fts5_unavailable", hint="跨 episode 召回已关闭")
        return
    try:
        conn.executescript(_FTS_DDL)
    except sqlite3.OperationalError as exc:
        logger.warning("fts5_schema_failed", error=str(exc))
        return
    _rebuild_if_empty(conn, "messages", "messages_fts")
    _rebuild_if_empty(conn, "turn_summaries", "turn_summaries_fts")
    _rebuild_if_empty(conn, "artifacts", "artifacts_fts")


def _rebuild_if_empty(conn: sqlite3.Connection, source: str, fts: str) -> None:
    """正文有行而倒排是空的（升级后第一次）才 rebuild，避免每次启动全表重建。"""
    try:
        src_n = conn.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]
        fts_n = conn.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
    except sqlite3.Error:
        return
    if src_n and not fts_n:
        try:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            logger.info("fts5_rebuilt", table=fts, rows=src_n)
        except sqlite3.Error as exc:
            logger.warning("fts5_rebuild_failed", table=fts, error=str(exc))
