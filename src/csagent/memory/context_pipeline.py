"""上下文组装管线：统一 Agent._handle_inner 中散落的 8 个步骤。

替代原来 agent.py 里 300 行的 _handle_inner 过程式编排。
每个 stage 接收 ContextFrame（含剩余 token budget），产出追加内容。

使用方式：
    pipeline = ContextPipeline(...)
    frame = pipeline.build(session_id, user_text, system_prompt, tools)
    # frame.messages → 发给 ReactLoop
    # frame.checkpoint_summary → 独立传递
    # frame.context_summary → 动态尾部
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from csagent.memory.budget import BudgetAllocation, ContextBudgetAllocator
from csagent.memory.compaction_policy import HistoryCompactionPolicy
from csagent.memory.compactor import CompactedContext
from csagent.memory.context_selector import QueryAwareContextSelector
from csagent.memory.prompt_layout import PromptLayoutOptimizer
from csagent.memory.protocols import SessionSnapshotStoreProtocol, ShortTermMemoryStore
from csagent.memory.token_counter import TokenCounter
from csagent.tools.preference_tool import ReplyPreferenceStore, preference_key

logger = structlog.get_logger(__name__)


@dataclass
class ContextFrame:
    """上下文管线的输出产物。"""

    # 给 ReactLoop 的参数
    context_summary: str = ""  # 动态尾部（最近对话 + 偏好 + 工具流水）
    checkpoint_summary: str = ""  # BP3 Checkpoint（独立传递）
    persistent_instructions: str = ""  # BP2 持久指令
    allowed_tools: list[dict[str, Any]] = field(default_factory=list)  # 确定性排序后的工具

    # 调试/审计信息
    allocation: BudgetAllocation | None = None
    needs_compact: bool = False


class ContextPipeline:
    """上下文组装管线：将 agent.py 的步骤 1-8 统一为声明式管线。

    每个 stage 是一个方法，按顺序执行：
    1. load_history         — 从 ShortTermMemory 加载当前 episode 对话
    2. allocate_budget      — 计算 Token 预算分配
    3. build_compacted      — L0 最近原文 + L2 Checkpoint
    4. inject_l1            — 本段更早轮次的 L1 摘要
    5. inject_preferences   — 注入回复偏好
    6. select_tool_history  — 筛选工具调用历史
    7. inject_todos         — 当前未完成待办（M3.5）
    8. inject_recall        — 跨 episode FTS5 召回（M3.4）
    9. assemble             — 组装最终 ContextFrame

    NOTE(M0.2): 原先在第 2 步和第 6 步之间还有「加载领域快照」与「查询感知筛选
    快照」两个 stage。领域快照从未被写入过，2026-09 整层删除。
    """

    def __init__(
        self,
        short_term_memory: ShortTermMemoryStore,
        snapshot_store: SessionSnapshotStoreProtocol | None = None,
        history_compactor: Any = None,
        token_counter: TokenCounter | None = None,
        budget_allocator: ContextBudgetAllocator | None = None,
        context_selector: QueryAwareContextSelector | None = None,
        prompt_layout: PromptLayoutOptimizer | None = None,
        reply_preference_store: ReplyPreferenceStore | None = None,
        persistent_instructions: str = "",
        history_window: int = 4,
        compaction_policy: HistoryCompactionPolicy | None = None,
        graded_memory: Any = None,
        recaller: Any = None,
    ) -> None:
        self._short_term = short_term_memory
        self._snapshot_store = snapshot_store
        self._compactor = history_compactor
        self._counter = token_counter or TokenCounter()
        self._budget = budget_allocator or ContextBudgetAllocator(self._counter)
        self._selector = context_selector or QueryAwareContextSelector(self._counter)
        self._layout = prompt_layout or PromptLayoutOptimizer(self._counter)
        self._pref_store = reply_preference_store
        self._persistent_instructions = (persistent_instructions or "").strip()
        self._history_window = max(1, history_window)
        # 压缩策略：与 ReactLoop 共用同一实例，保证「循环外 LLM 摘要」和
        # 「循环内水位兜底」用的是同一条水位线（见 memory/compaction_policy.py）
        self._compaction = compaction_policy or HistoryCompactionPolicy(self._counter)
        self._graded = graded_memory
        self._recaller = recaller

    def build(
        self,
        session_id: str,
        user_text: str,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]],
        scope: Any = None,
    ) -> ContextFrame:
        """执行完整的上下文组装管线，返回 ContextFrame。"""
        frame = ContextFrame()
        frame.persistent_instructions = self._persistent_instructions

        # 1. 加载历史（sqlite 后端已是当前 episode）
        history = self._short_term.load(session_id)
        episode_id = ""
        current_episode = getattr(self._short_term, "current_episode_id", None)
        if callable(current_episode):
            episode_id = current_episode(session_id) or ""

        # 2. 工具排序（确定性，最大化 KV Cache）
        frame.allowed_tools = self._layout.sort_tool_schemas(tool_schemas)

        # 3. 计算预算
        cached_summary = ""
        if self._graded is not None and episode_id:
            l2_row = getattr(self._short_term, "latest_checkpoint", None)
            if callable(l2_row):
                row = l2_row(episode_id)
                if row is not None:
                    cached_summary = row.summary or ""
        if not cached_summary:
            peek = getattr(self._compactor, "peek_cached_summary", None)
            if callable(peek) and session_id:
                cached_summary = peek(session_id) or ""

        frame.allocation = self._budget.allocate(
            system_prompt=system_prompt,
            tools=frame.allowed_tools,
            recent_history=history[-self._history_window:] if history else [],
            summary=cached_summary or None,
        )
        frame.needs_compact = frame.allocation.needs_compact

        # 4. 构建压缩上下文
        compacted = self._build_compacted(
            history, session_id,
            current_query=user_text,
            token_budget=frame.allocation.summary_tokens,
            recent_token_budget=frame.allocation.recent_history_tokens,
        )
        # L2 落盘后优先用它，避免和进程内 Checkpoint 缓存各说各话。
        if self._graded is not None and episode_id:
            l2 = self._graded.format_l2_block(
                episode_id, token_budget=frame.allocation.summary_tokens
            )
            if l2:
                compacted.summary_block = l2
        frame.checkpoint_summary = compacted.summary_block

        # 组装动态部分
        dynamic_parts: list[str] = []
        if compacted.recent_block:
            dynamic_parts.append(compacted.recent_block)

        # 4b. 本段 L0 窗口之外的 L1 轮次摘要
        if self._graded is not None and episode_id:
            window_turns = max(1, (self._history_window + 1) // 2)
            list_summaries = getattr(self._short_term, "list_turn_summaries", None)
            if callable(list_summaries):
                rows = list_summaries(episode_id)
                if rows:
                    before_turn = max(row.turn_id for row in rows) - window_turns + 1
                    l1 = self._graded.format_l1_block(
                        episode_id,
                        before_turn=max(1, before_turn),
                        token_budget=800,
                    )
                    if l1:
                        dynamic_parts.append(l1)

        # 5. 回复偏好
        if self._pref_store is not None and scope is not None:
            pref_block = self._pref_store.format_for_context(preference_key(scope))
            if pref_block:
                dynamic_parts.append(pref_block)

        # 6. 工具调用历史
        #
        # 预算取自 BudgetAllocation 的独立配额（chat.tool_history_tokens）。
        # M0.2 之前这里写的是 `snapshots_tokens // 4 or 80`——预算从领域快照派生，
        # 而快照恒空导致它被永久钉死在 80 token、只能注入约 2 条。
        load_tool_calls = getattr(self._snapshot_store, "load_tool_calls", None)
        if load_tool_calls is not None:
            tool_budget = frame.allocation.tool_history_tokens
            if tool_budget > 0:
                # 每条注入时截到 40 token，按预算反推该捞多少条，再留一倍余量给筛选
                max_items = max(1, tool_budget // 40)
                tool_history = load_tool_calls(session_id, last_n=max_items * 2)
                tool_lines = self._selector.select_tool_history(
                    query=user_text,
                    tool_calls=tool_history,
                    token_budget=tool_budget,
                    max_items=max_items,
                )
                if tool_lines:
                    dynamic_parts.append("## 最近工具调用\n" + "\n".join(tool_lines))

        # 7. 未完成待办：长任务进度由确定性数据回答（M3.5）
        try:
            from csagent.tools.packs.todo_pack import format_open_todos_for_context

            todo_block = format_open_todos_for_context(session_id)
            if todo_block:
                dynamic_parts.append(todo_block)
        except Exception as exc:  # noqa: BLE001
            logger.warning("todo_inject_failed", session_id=session_id, error=str(exc))

        # 8. 跨段召回：当前窗口是空的（新 episode）或用户在问更早的事
        if self._recaller is not None:
            try:
                recall_block = self._recaller.format_block(
                    session_id,
                    user_text,
                    exclude_episode_id=episode_id,
                    token_budget=400,
                )
                if recall_block:
                    dynamic_parts.append(recall_block)
            except Exception as exc:  # noqa: BLE001
                logger.warning("recall_inject_failed", session_id=session_id, error=str(exc))

        frame.context_summary = "\n\n".join(dynamic_parts)

        # 水位线触发压缩
        if frame.needs_compact and self._compactor is not None:
            try:
                self._compactor.schedule_refresh(session_id, history, focus=user_text)
            except TypeError:
                self._compactor.schedule_refresh(session_id, history)
            logger.info(
                "context_compact_triggered",
                session_id=session_id,
                total_budget=frame.allocation.total_budget,
                compact_threshold=self._compaction.compact_threshold,
            )

        return frame

    def _build_compacted(
        self,
        history: list,
        session_id: str = "",
        *,
        current_query: str = "",
        token_budget: int | None = None,
        recent_token_budget: int | None = None,
    ) -> CompactedContext:
        """构建 Checkpoint + 最近对话；兼容旧版返回 str / 少参数的压缩器。"""
        if self._compactor is not None and session_id:
            build = self._compactor.build_context
            try:
                result = build(
                    session_id, history,
                    current_query=current_query,
                    token_budget=token_budget,
                    recent_token_budget=recent_token_budget,
                )
            except TypeError:
                try:
                    result = build(
                        session_id, history,
                        current_query=current_query,
                        token_budget=token_budget,
                    )
                except TypeError:
                    result = build(session_id, history, current_query=current_query)
            if isinstance(result, CompactedContext):
                return result
            text = str(result or "")
            return CompactedContext(recent_block=text, total_tokens=self._counter.count_text(text))
        recent = history[-self._history_window:] if history else []
        if not recent:
            return CompactedContext()
        lines = [f"{m.role}: {m.content or ''}" for m in recent]
        text = "## 最近对话\n" + "\n".join(lines)
        if token_budget is not None:
            text = self._counter.truncate_to_tokens(text, token_budget)
        return CompactedContext(
            recent_block=text,
            total_tokens=self._counter.count_text(text),
        )
