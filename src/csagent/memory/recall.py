"""跨 episode FTS5 召回（M3.4）。

存储永不删除之后，召回必须自己划边界，三条缺一不可：

1. 默认只搜最近 ``recall_window_days`` 天；问句带明确时间指向才放开。
2. 注入片段带该轮日期，避免把旧数值当成当前值。
3. BM25 按时间衰减，同等相关度下近期优先。

打分 = ``-bm25 × 1`` + 实体精确命中加权 + 新鲜度。embedding 不做。
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

from csagent.memory.sqlite_fts import fts5_available
from csagent.memory.sqlite_schema import connect
from csagent.memory.token_counter import TokenCounter
from csagent.prompts.domain_text import extract_domain_entities

logger = structlog.get_logger(__name__)

# 问句里出现这些，视为「用户在指过去某一刻」，才把时间窗放到全量。
# 用通用时间词，不写任何行业口径。
_TIME_POINTING_RE = re.compile(
    r"去年|前年|今年|最早|上次|那天|上[个個]月|上月|\d{4}\s*年|\d{1,2}\s*月|那次|当时"
)

_HALF_LIFE_DAYS = 30.0
_ENTITY_BOOST = 4.0


@dataclass(frozen=True)
class RecallHit:
    """一条召回结果，注入前再格式化。"""

    kind: str  # message | turn_summary | artifact
    session_id: str
    episode_id: str
    created_at: float
    text: str
    score: float


class EpisodeRecaller:
    """对当前身份的历史 episode 做 BM25 召回。"""

    def __init__(
        self,
        db_path: str | Path,
        token_counter: TokenCounter | None = None,
        *,
        window_days: float = 180.0,
        max_hits: int = 4,
        snippet_tokens: int = 80,
    ) -> None:
        self._path = Path(db_path)
        self._counter = token_counter or TokenCounter()
        self._window_days = max(0.0, window_days)
        self._max_hits = max(1, max_hits)
        self._snippet_tokens = max(20, snippet_tokens)

    def format_block(
        self,
        session_id: str,
        query: str,
        *,
        exclude_episode_id: str = "",
        token_budget: int = 400,
    ) -> str:
        """把命中拼成可注入的段落；没有命中返回空串。"""
        hits = self.search(
            session_id, query, exclude_episode_id=exclude_episode_id
        )
        if not hits or token_budget <= 0:
            return ""
        lines = ["## 跨段召回（带日期；数值可能已过期，需要时请重查）"]
        used = self._counter.count_text(lines[0])
        for hit in hits:
            day = _format_day(hit.created_at)
            snippet = self._counter.truncate_to_tokens(hit.text, self._snippet_tokens)
            line = f"- [{day}] {snippet}"
            cost = self._counter.count_text(line)
            if used + cost > token_budget:
                break
            lines.append(line)
            used += cost
        return "\n".join(lines) if len(lines) > 1 else ""

    def search(
        self,
        session_id: str,
        query: str,
        *,
        exclude_episode_id: str = "",
        now: float | None = None,
    ) -> list[RecallHit]:
        """返回按综合分排序的命中，已去掉当前 episode。"""
        match = _fts_match(query)
        if not session_id or not match:
            return []
        at = time.time() if now is None else now
        cutoff = 0.0
        if self._window_days > 0 and not _looks_time_pointing(query):
            cutoff = at - self._window_days * 86400
        entities = extract_domain_entities(query)
        hits: list[RecallHit] = []
        try:
            with closing(connect(self._path)) as conn:
                if not fts5_available(conn):
                    return []
                hits.extend(
                    self._search_messages(
                        conn, session_id, match, entities, cutoff, exclude_episode_id, at
                    )
                )
                hits.extend(
                    self._search_summaries(
                        conn, session_id, match, entities, cutoff, exclude_episode_id, at
                    )
                )
                hits.extend(
                    self._search_artifacts(
                        conn, session_id, match, entities, cutoff, exclude_episode_id, at
                    )
                )
        except sqlite3.Error as exc:
            logger.warning("recall_search_failed", session_id=session_id, error=str(exc))
            return []
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: self._max_hits]

    def _search_messages(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        match: str,
        entities: set[str],
        cutoff: float,
        exclude_episode_id: str,
        now: float,
    ) -> list[RecallHit]:
        rows = conn.execute(
            "SELECT m.session_id, m.episode_id, m.created_at, m.content,"
            " bm25(messages_fts) AS rank"
            " FROM messages_fts"
            " JOIN messages m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ? AND m.session_id = ?"
            " AND m.created_at >= ? AND m.episode_id != ?"
            " ORDER BY rank LIMIT 12",
            (match, session_id, cutoff, exclude_episode_id),
        ).fetchall()
        return [
            _to_hit("message", sid, epid, created, text or "", rank, entities, now)
            for sid, epid, created, text, rank in rows
        ]

    def _search_summaries(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        match: str,
        entities: set[str],
        cutoff: float,
        exclude_episode_id: str,
        now: float,
    ) -> list[RecallHit]:
        rows = conn.execute(
            "SELECT t.session_id, t.episode_id, t.created_at, t.summary,"
            " bm25(turn_summaries_fts) AS rank"
            " FROM turn_summaries_fts"
            " JOIN turn_summaries t ON t.rowid = turn_summaries_fts.rowid"
            " WHERE turn_summaries_fts MATCH ? AND t.session_id = ?"
            " AND t.created_at >= ? AND t.episode_id != ?"
            " ORDER BY rank LIMIT 12",
            (match, session_id, cutoff, exclude_episode_id),
        ).fetchall()
        return [
            _to_hit("turn_summary", sid, epid, created, text or "", rank, entities, now)
            for sid, epid, created, text, rank in rows
        ]

    def _search_artifacts(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        match: str,
        entities: set[str],
        cutoff: float,
        exclude_episode_id: str,
        now: float,
    ) -> list[RecallHit]:
        rows = conn.execute(
            "SELECT a.session_id, a.episode_id, a.created_at, a.filename,"
            " bm25(artifacts_fts) AS rank"
            " FROM artifacts_fts"
            " JOIN artifacts a ON a.rowid = artifacts_fts.rowid"
            " WHERE artifacts_fts MATCH ? AND a.session_id = ?"
            " AND a.created_at >= ? AND a.episode_id != ?"
            " ORDER BY rank LIMIT 8",
            (match, session_id, cutoff, exclude_episode_id),
        ).fetchall()
        hits: list[RecallHit] = []
        for sid, epid, created, name, rank in rows:
            hits.append(
                _to_hit(
                    "artifact",
                    sid,
                    epid or "",
                    created,
                    f"文件 {name or ''}",
                    rank,
                    entities,
                    now,
                )
            )
        return hits


def _to_hit(
    kind: str,
    session_id: str,
    episode_id: str,
    created_at: float,
    text: str,
    bm25_rank: float,
    entities: set[str],
    now: float,
) -> RecallHit:
    # FTS5 的 bm25() 越小（更负）越好，取负号变成越大越好。
    score = -float(bm25_rank or 0.0)
    if entities and entities & extract_domain_entities(text):
        score += _ENTITY_BOOST
    age_days = max(0.0, (now - float(created_at or 0.0)) / 86400.0)
    score *= math.pow(0.5, age_days / _HALF_LIFE_DAYS)
    return RecallHit(
        kind=kind,
        session_id=session_id,
        episode_id=str(episode_id or ""),
        created_at=float(created_at or 0.0),
        text=text.strip(),
        score=score,
    )


def _looks_time_pointing(query: str) -> bool:
    return bool(_TIME_POINTING_RE.search(query or ""))


def _fts_match(query: str) -> str:
    """把自然语言压成 FTS5 MATCH 表达式：标识 + 英文词 + 中文 2-gram，OR 连接。"""
    terms: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        token = raw.strip()
        if len(token) < 2 or token in seen:
            return
        if any(ch in token for ch in '"*^:'):
            token = re.sub(r'["*^:]', " ", token).strip()
        if len(token) < 2:
            return
        seen.add(token)
        terms.append(f'"{token}"')

    for ent in extract_domain_entities(query):
        _add(ent)
    for word in re.findall(r"[A-Za-z0-9_]{2,}", query or ""):
        _add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", query or ""):
        if len(run) == 1:
            continue
        for i in range(min(len(run) - 1, 8)):
            _add(run[i : i + 2])
    return " OR ".join(terms[:24])


def _format_day(ts: float) -> str:
    if ts <= 0:
        return "未知日期"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
