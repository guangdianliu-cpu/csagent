"""自适应上下文预算分配器：按分层优先级为各组件动态分配 Token 预算。

分配策略
--------
总预算 = model_context_limit × compact_threshold
可分配 = 总预算 - system_prompt - tools - response_reserve

Tier 0 (固定):   System Prompt + Tool Schemas
工具流水 (固定):  最近工具调用摘要，固定小额先扣，见下
Tier 1 (优先):   最近对话原文（至少保证最近 2 条）
Tier 2 (可压缩): 历史 Checkpoint（剩余空间）
Reserve:         工具返回 + 模型响应

NOTE(M0.2): 原先在 Tier 1 与 Checkpoint 之间还有一层「领域快照」，占 30% 的
可分配空间。该层的写入方法从未被调用过，供给恒为空，2026-09 整层删除。
更糟的是工具流水的预算曾经从快照预算派生（``snapshots_tokens // 4 or 80``），
快照恒空导致它被永久钉死在 80 token；现在它是一份独立的固定配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from csagent.memory.token_counter import TokenCounter

logger = structlog.get_logger(__name__)


@dataclass
class BudgetAllocation:
    """各上下文组件的 Token 预算分配结果。"""

    system_prompt_tokens: int = 0
    tools_tokens: int = 0
    recent_history_tokens: int = 0
    # 最近工具调用摘要的注入预算（固定配额，不随其他层浮动）
    tool_history_tokens: int = 0
    summary_tokens: int = 0
    response_reserve: int = 0
    total_budget: int = 0
    needs_compact: bool = False

    def as_audit_dict(self) -> dict[str, int | bool]:
        """给审计 JSONL 用的一层一行占用。

        ``summary_tokens`` 在分配器里是 Checkpoint 的截断**上限**，不是当前摘要的
        实测长度；实测长度由 Agent 在管线组装后再补进 ``checkpoint`` 字段。
        """
        return {
            "system": self.system_prompt_tokens,
            "tools": self.tools_tokens,
            "recent": self.recent_history_tokens,
            "tool_history": self.tool_history_tokens,
            "summary_budget": self.summary_tokens,
            "reserve": self.response_reserve,
            "total_budget": self.total_budget,
            "needs_compact": self.needs_compact,
        }

    @property
    def available_for_content(self) -> int:
        """除去固定开销和预留后，可用于内容的总 Token 数。"""
        return max(
            0,
            self.total_budget
            - self.system_prompt_tokens
            - self.tools_tokens
            - self.response_reserve,
        )


@dataclass
class ContextBudgetConfig:
    """预算分配配置。"""

    # 超过此水位线触发压缩
    compact_threshold: float = 0.80
    # 预留给模型生成的比例
    response_reserve_ratio: float = 0.10
    # 最近对话占可分配空间的比例上限；Checkpoint 拿剩余全部
    tier1_ratio: float = 0.50
    # 最近对话至少保证的 Token 数（2 条短消息）
    tier1_min_tokens: int = 200
    # 最近工具调用摘要的固定配额：在分层之前先扣，保证它不被对话挤没。
    # 约 15 条（select_tool_history 每条截到 40 token）。
    tool_history_tokens: int = 600


class ContextBudgetAllocator:
    """根据模型窗口大小和各组件实际内容，动态分配 Token 预算。

    核心原则：
    1. 固定开销先扣除（System Prompt + Tools + Response Reserve）
    2. 剩余空间按优先级分配
    3. 各 Tier 实际使用不超过分配上限；未用满的空间下放给低优先级
    """

    def __init__(
        self,
        token_counter: TokenCounter,
        config: ContextBudgetConfig | None = None,
    ) -> None:
        self._counter = token_counter
        self._config = config or ContextBudgetConfig()

    @property
    def token_counter(self) -> TokenCounter:
        return self._counter

    def allocate(
        self,
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        recent_history: list | None = None,
        summary: str | None = None,
    ) -> BudgetAllocation:
        """计算各组件的 Token 预算。

        实际消耗 ≤ 分配的 Token 数，未用满的空间自动下放给下一层。

        :param system_prompt: 本轮系统提示词全文（Tier 0 固定开销）
        :param tools: 本轮可见的工具 schema 列表（Tier 0 固定开销）
        :param recent_history: 最近对话消息，用于估算 Tier 1 实际需要多少
        :param summary: 已缓存的 Checkpoint，只用于判断是否需要压缩
        :returns: 各层预算；``needs_compact`` 为真时调用方应触发后台压缩
        """
        cfg = self._config
        model_limit = self._counter.model_context_limit
        total_budget = int(model_limit * cfg.compact_threshold)
        response_reserve = int(model_limit * cfg.response_reserve_ratio)

        # Tier 0: 固定开销
        sys_tokens = self._counter.count_text(system_prompt) if system_prompt else 0
        tools_tokens = self._counter.count_tools(tools) if tools else 0

        # 可分配空间
        available = max(0, total_budget - sys_tokens - tools_tokens - response_reserve)

        # 工具流水：固定小额，在按比例分层之前先扣。
        # 放在最前面是因为它是「这一轮之前做过什么」的唯一确定性来源，
        # 被对话或摘要挤掉的代价远大于它占的那几百 token。
        tool_history_used = min(cfg.tool_history_tokens, available)
        remaining = available - tool_history_used

        # Tier 1: 最近对话
        tier1_cap = max(cfg.tier1_min_tokens, int(remaining * cfg.tier1_ratio))
        tier1_cap = min(tier1_cap, remaining)
        tier1_actual = 0
        if recent_history:
            # 计算实际需要的 Token 数
            for msg in recent_history:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "user")
                tier1_actual += self._counter.count_text(f"{role}: {content}") + 4
        tier1_used = min(tier1_actual, tier1_cap)

        # Tier 2: 历史 Checkpoint 占用「剩余全部」作为截断上限
        #（即使当前尚无 Checkpoint 也预留）
        summary_actual = self._counter.count_text(summary) if summary else 0
        summary_budget = max(0, remaining - tier1_used)

        # 压缩信号看实际占用，不看预留上限（否则预留满额会永远 needs_compact）
        actual_used = (
            sys_tokens + tools_tokens + response_reserve
            + tool_history_used + tier1_used + summary_actual
        )
        needs_compact = actual_used >= total_budget

        allocation = BudgetAllocation(
            system_prompt_tokens=sys_tokens,
            tools_tokens=tools_tokens,
            recent_history_tokens=tier1_used,
            tool_history_tokens=tool_history_used,
            summary_tokens=summary_budget,
            response_reserve=response_reserve,
            total_budget=total_budget,
            needs_compact=needs_compact,
        )

        logger.debug(
            "context_budget_allocated",
            model_limit=model_limit,
            total_budget=total_budget,
            system=sys_tokens,
            tools=tools_tokens,
            recent=tier1_used,
            tool_history=tool_history_used,
            summary=summary_budget,
            reserve=response_reserve,
            needs_compact=needs_compact,
        )
        return allocation

    def should_compact(self, current_tokens: int) -> bool:
        """给定当前已使用的 Token 数，判断是否需要触发压缩。"""
        threshold = int(self._counter.model_context_limit * self._config.compact_threshold)
        return current_tokens >= threshold
