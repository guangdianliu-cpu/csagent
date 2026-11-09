"""L1 轮次摘要 + L2 episode Checkpoint（M3.3）。

替代「窗口外全部历史 → 一个 400 token 摘要」：
- L1：每轮结束后后台写一条约 200 token 的摘要，落 ``turn_summaries``
- L2：每 N 条 L1 合并为一份 episode Checkpoint，落 ``checkpoints``

两条都遵守压缩器的数值纪律：标识保留、具体数字丢掉。LLM 不可用时走启发式
兜底，保证没模型的测试和断网环境也能产出可注入的文本。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextvars import Context
from typing import Any

import structlog

from csagent.common.keyed_lock import KeyedLocks

from csagent.memory.sqlite_store import SqliteMemoryStore, TurnSummaryRow
from csagent.memory.token_counter import TokenCounter
from csagent.prompts.domain_text import extract_domain_entities

logger = structlog.get_logger(__name__)

# 看起来像「精确数值」的片段：小数、百分数、四位及以上纯数字。
# 短代码（如 6 位标识）由 extract_domain_entities 保住，不在这里切。
_NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\d+\.\d+|\d{4,})%?")

L1_SYSTEM_PROMPT = """你是单轮对话压缩器。把这一轮提炼成不超过 200 token 的摘要。

只写：用户问了什么、做了什么、得出什么结论、还剩什么没做。
标识（代码、名称、路径、artifact_id）必须保留。
具体数字必须丢掉，改写成来源指针（「见本轮工具记录 / read_artifact」）。
不要寒暄，不要列表以外的格式。"""


def heuristic_turn_summary(
    *,
    turn_id: int,
    user_text: str,
    assistant_text: str,
    token_counter: TokenCounter,
    max_tokens: int = 200,
) -> str:
    """LLM 不可用时的 L1 兜底：保住标识，丢掉像数值的片段。

    :param turn_id: 本轮序号，写进摘要方便指向「第 N 轮」
    :param user_text: 用户原文
    :param assistant_text: 助手原文
    :param token_counter: 用于截断
    :param max_tokens: L1 预算
    :returns: 可直接注入的一小段中文
    """
    blob = f"{user_text or ''} {assistant_text or ''}"
    entities = sorted(extract_domain_entities(blob))
    keep = set(entities)

    def _clean(text: str) -> str:
        out = text or ""
        for match in _NUMERIC_RE.findall(out):
            if match not in keep:
                out = out.replace(match, "")
        return re.sub(r"\s+", " ", out).strip()

    user_bit = _clean(user_text)[:160]
    parts = [f"第 {turn_id} 轮：用户问了 {user_bit or '一项任务'}。"]
    if entities:
        parts.append("涉及标识：" + "、".join(entities[:8]) + "。")
    parts.append("具体数值见本轮工具记录或 artifact，不要凭本条复述数字。")
    return token_counter.truncate_to_tokens(" ".join(parts), max_tokens)


class GradedMemory:
    """L1/L2 的生成、落盘与注入文本装配。"""

    def __init__(
        self,
        store: SqliteMemoryStore,
        llm_client: Any = None,
        token_counter: TokenCounter | None = None,
        *,
        l1_max_tokens: int = 200,
        l2_merge_every: int = 8,
        l2_max_tokens: int = 4000,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._llm = llm_client
        self._counter = token_counter or TokenCounter()
        self._l1_max = max(40, l1_max_tokens)
        self._merge_every = max(2, l2_merge_every)
        self._l2_max = max(200, l2_max_tokens)
        self._enabled = enabled
        # 用完即删，不随历史 episode 数增长（见 common/keyed_lock.py）
        self._locks = KeyedLocks()

    def schedule_after_turn(
        self,
        session_id: str,
        *,
        episode_id: str,
        turn_id: int,
        user_text: str,
        assistant_text: str,
        created_at: float | None = None,
    ) -> None:
        """回写后触发：有事件循环就后台跑，否则本线程同步写启发式摘要。"""
        if not self._enabled or not episode_id or turn_id <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_l1_sync(
                session_id,
                episode_id=episode_id,
                turn_id=turn_id,
                user_text=user_text,
                assistant_text=assistant_text,
                created_at=created_at,
            )
            return
        loop.create_task(
            self._write_l1(
                session_id,
                episode_id=episode_id,
                turn_id=turn_id,
                user_text=user_text,
                assistant_text=assistant_text,
                created_at=created_at,
            ),
            # 空 context：不要把本请求的 audit_trace_var 带进后台 L1 LLM（M4.1）。
            context=Context(),
        )

    def format_l1_block(
        self,
        episode_id: str,
        *,
        before_turn: int,
        token_budget: int = 800,
    ) -> str:
        """当前 episode 里、L0 窗口之前的轮次摘要，拼成一段注入文本。"""
        rows = [
            row
            for row in self._store.list_turn_summaries(episode_id)
            if row.turn_id < before_turn
        ]
        if not rows or token_budget <= 0:
            return ""
        lines = ["## 本段更早轮次摘要"]
        used = self._counter.count_text(lines[0])
        for row in rows:
            line = f"- 第 {row.turn_id} 轮：{row.summary}"
            cost = self._counter.count_text(line)
            if used + cost > token_budget:
                break
            lines.append(line)
            used += cost
        return "\n".join(lines) if len(lines) > 1 else ""

    def format_l2_block(self, episode_id: str, *, token_budget: int = 4000) -> str:
        """当前 episode 最新 Checkpoint；没有则空串。"""
        row = self._store.latest_checkpoint(episode_id)
        if row is None or not row.summary.strip():
            return ""
        body = self._counter.truncate_to_tokens(row.summary, token_budget)
        return (
            f"## 本段 Checkpoint（覆盖第 {row.covers_from}–{row.covers_to} 轮）\n"
            f"{body}"
        )

    def _write_l1_sync(self, session_id: str, **kwargs: Any) -> None:
        """无事件循环时只写启发式，不调 LLM。"""
        summary = heuristic_turn_summary(
            turn_id=int(kwargs["turn_id"]),
            user_text=str(kwargs.get("user_text") or ""),
            assistant_text=str(kwargs.get("assistant_text") or ""),
            token_counter=self._counter,
            max_tokens=self._l1_max,
        )
        self._store.upsert_turn_summary(
            episode_id=str(kwargs["episode_id"]),
            turn_id=int(kwargs["turn_id"]),
            session_id=session_id,
            summary=summary,
            tokens=self._counter.count_text(summary),
            created_at=kwargs.get("created_at"),
        )
        self._maybe_merge_l2_sync(str(kwargs["episode_id"]))

    async def _write_l1(
        self,
        session_id: str,
        *,
        episode_id: str,
        turn_id: int,
        user_text: str,
        assistant_text: str,
        created_at: float | None,
    ) -> None:
        async with self._locks.hold(episode_id):
            summary = await self._summarize_turn(turn_id, user_text, assistant_text)
            self._store.upsert_turn_summary(
                episode_id=episode_id,
                turn_id=turn_id,
                session_id=session_id,
                summary=summary,
                tokens=self._counter.count_text(summary),
                created_at=created_at,
            )
            await self._maybe_merge_l2(episode_id)

    async def _summarize_turn(
        self, turn_id: int, user_text: str, assistant_text: str
    ) -> str:
        fallback = heuristic_turn_summary(
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
            token_counter=self._counter,
            max_tokens=self._l1_max,
        )
        if self._llm is None:
            return fallback
        user = (
            f"第 {turn_id} 轮\n"
            f"用户：{self._counter.truncate_to_tokens(user_text or '', 400)}\n"
            f"助手：{self._counter.truncate_to_tokens(assistant_text or '', 400)}\n"
            f"请按规则输出摘要，预算 {self._l1_max} token。"
        )
        try:
            text = await self._llm.chat(
                [
                    {"role": "system", "content": L1_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                json_mode=False,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("l1_summarize_failed", turn_id=turn_id, error=str(exc))
            return fallback
        cleaned = (text or "").strip()
        if not cleaned:
            return fallback
        return self._counter.truncate_to_tokens(cleaned, self._l1_max)

    def _uncovered_l1(self, episode_id: str) -> list[TurnSummaryRow]:
        rows = self._store.list_turn_summaries(episode_id)
        latest = self._store.latest_checkpoint(episode_id)
        covered_to = latest.covers_to if latest is not None else 0
        return [row for row in rows if row.turn_id > covered_to]

    def _maybe_merge_l2_sync(self, episode_id: str) -> None:
        pending = self._uncovered_l1(episode_id)
        if len(pending) < self._merge_every:
            return
        batch = pending[: self._merge_every]
        latest = self._store.latest_checkpoint(episode_id)
        incoming = self._heuristic_l2(batch)
        existing = latest.summary if latest is not None else ""
        text = self._compose_l2(existing, incoming)
        self._store.upsert_checkpoint(
            episode_id=episode_id,
            seq=1,
            covers_from=1 if latest is None else latest.covers_from,
            covers_to=batch[-1].turn_id,
            summary=text,
            source_digest=_digest_rows(batch),
        )

    async def _maybe_merge_l2(self, episode_id: str) -> None:
        pending = self._uncovered_l1(episode_id)
        if len(pending) < self._merge_every:
            return
        batch = pending[: self._merge_every]
        latest = self._store.latest_checkpoint(episode_id)
        existing = latest.summary if latest is not None else ""
        text = await self._summarize_l2(batch, existing=existing)
        self._store.upsert_checkpoint(
            episode_id=episode_id,
            seq=1,
            covers_from=1 if latest is None else latest.covers_from,
            covers_to=batch[-1].turn_id,
            summary=text,
            source_digest=_digest_rows(batch),
        )

    def _compose_l2(self, existing: str, incoming: str) -> str:
        """把新一批叠进已有 Checkpoint，超预算时头部留旧的、尾部保住新的。

        ``truncate_to_tokens(旧 + 新)`` 只留头部：第一次填满之后最新内容永远进不来，
        Checkpoint 冻在最老一段上。拆预算避免这件事——新内容最多占 2/3，剩余给
        旧摘要的开头（早期标识）。

        :param existing: 已有 Checkpoint 正文；没有则为空
        :param incoming: 本批 L1 压成的新摘要
        :returns: 不超过 ``l2_max`` token 的合并文本
        """
        incoming = (incoming or "").strip()
        existing = (existing or "").strip()
        if not existing:
            return self._counter.truncate_to_tokens(incoming, self._l2_max)
        if not incoming:
            return self._counter.truncate_to_tokens(existing, self._l2_max)
        joined = existing + "\n" + incoming
        if self._counter.count_text(joined) <= self._l2_max:
            return joined
        new_cap = min(self._counter.count_text(incoming), (self._l2_max * 2) // 3)
        new_cap = max(1, min(new_cap, self._l2_max))
        new_part = self._counter.truncate_to_tokens(incoming, new_cap)
        rest = self._l2_max - self._counter.count_text(new_part)
        if rest <= 0:
            return new_part
        old_part = self._counter.truncate_to_tokens(existing, rest)
        return f"{old_part}\n{new_part}"

    def _heuristic_l2(self, rows: list[TurnSummaryRow]) -> str:
        joined = "\n".join(f"- 第 {r.turn_id} 轮：{r.summary}" for r in rows)
        return self._counter.truncate_to_tokens(joined, self._l2_max)

    async def _summarize_l2(
        self, rows: list[TurnSummaryRow], *, existing: str = ""
    ) -> str:
        """LLM 把新 L1 合并进已有 Checkpoint；失败则启发式拼接。

        :param rows: 尚未被 Checkpoint 覆盖的一轮或多轮 L1
        :param existing: 已有 Checkpoint；空表示本段第一份
        :returns: 不超过 ``l2_max`` 的合并摘要
        """
        incoming = self._heuristic_l2(rows)
        fallback = self._compose_l2(existing, incoming)
        if self._llm is None:
            return fallback
        body = "\n".join(f"第 {r.turn_id} 轮：{r.summary}" for r in rows)
        user_parts = []
        if existing.strip():
            user_parts.append(f"## 已有 Checkpoint\n{existing.strip()}")
        user_parts.append(f"## 新增轮次摘要\n{body}")
        user_parts.append(
            f"请在已有 Checkpoint 上做字段级合并更新，不要遗忘早期标识；"
            f"预算 {self._l2_max} token。不要复述具体数字。"
        )
        try:
            from csagent.memory.compactor import _load_compactor_system_prompt

            text = await self._llm.chat(
                [
                    {"role": "system", "content": _load_compactor_system_prompt()},
                    {"role": "user", "content": "\n\n".join(user_parts)},
                ],
                json_mode=False,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("l2_summarize_failed", error=str(exc))
            return fallback
        cleaned = (text or "").strip()
        if not cleaned:
            return fallback
        return self._counter.truncate_to_tokens(cleaned, self._l2_max)


def _digest_rows(rows: list[TurnSummaryRow]) -> str:
    raw = "|".join(f"{r.turn_id}:{r.summary}" for r in rows)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
