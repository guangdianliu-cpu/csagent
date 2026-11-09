"""历史压缩的唯一策略持有者：循环外摘要与循环内兜底共用一套水位线。

问题背景（内核审视报告问题 E）
------------------------------
改造前项目有两条互不知情的压缩链路：

- **Agent 层**：``ContextPipeline`` 每轮开始前调 ``StructuredCompactor`` 做 LLM
  摘要，产出 Checkpoint，水位线取自 ``ContextBudgetConfig.compact_threshold``。
- **ReactLoop 层**：``_maybe_compact_history`` 在循环内对 messages 数组做字符串
  截断式摘要，自己另存一份 ``compact_threshold``，还靠「content 里有没有
  『Checkpoint』这个词」来猜哪些头部消息不能动。

两者阈值各配各的、保护区靠字符串嗅探，改一处 Prompt 文案就可能让循环内压缩
啃掉 Checkpoint 或本轮问题。本模块把「什么时候压、压哪一段」收归一处：

- 两条链路共用同一个 ``TokenCounter`` 与 ``compact_threshold``（由 main.py 构造
  一次，注入给 ``Agent`` 和 ``ReactLoop``）；
- 头部保护区由调用方显式传入 ``protected_head``（来自
  ``PromptLayoutOptimizer.initial_head_count``），不再猜。

循环内压缩为什么没有按报告建议直接删掉
--------------------------------------
报告建议「循环内只做 tool result 截断，不做 messages 级压缩」。但本项目
``react_max_rounds`` 默认 24，单条 tool result 上限 8000 字符且可并行 5 个，
循环跑满时 messages 完全可能撑爆上下文窗口——而这些消息是在循环**内部**才产生
的，Agent 层进循环前压不到。所以这里保留循环内压缩，但把它降级为纯水位兜底：

- 有 TokenCounter 时，**只**按 Token 水位触发（与 Agent 层同一条水位线）；
- 消息条数这条粗糙的经验规则只在没有 TokenCounter 时兜底，不再与 Token 预算
  抢着开火（改造前它无视 Token 余量也会触发，正是「没有协调机制」的具体表现）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from csagent.memory.token_counter import TokenCounter

logger = structlog.get_logger(__name__)

# 循环内压缩产物的标记：用于识别「上一次压缩的摘要」，避免摘要套娃
REACT_COMPACT_MARK = "## 早期对话摘要（ReAct 循环内自动压缩）"

# 保留最近 N 轮完整消息，更早的轮次做摘要（1 轮 ≈ assistant + tool 若干）
DEFAULT_RECENT_ROUNDS_KEEP = 5

# 没有 TokenCounter 时的兜底：消息数超过「保护区 + 尾窗 + 该值」才压
_NO_COUNTER_SLACK_MESSAGES = 24

# 没有 TokenCounter 时的默认上下文上限估算
_DEFAULT_CONTEXT_LIMIT = 64000


@dataclass
class CompactionDecision:
    """一次压缩判定的结果，便于日志/审计说清楚「为什么压」。"""

    should_compact: bool = False
    reason: str = "below_watermark"
    current_tokens: int = 0
    limit_tokens: int = 0


class HistoryCompactionPolicy:
    """会话历史压缩策略：循环外与循环内共用同一套水位线与保留窗口。

    只负责「压不压、压哪一段、怎么压」，不关心谁来调用。
    """

    def __init__(
        self,
        token_counter: TokenCounter | None = None,
        *,
        compact_threshold: float = 0.80,
        recent_rounds_keep: int = DEFAULT_RECENT_ROUNDS_KEEP,
    ) -> None:
        self._counter = token_counter
        self._threshold = compact_threshold
        self._recent_rounds_keep = max(1, int(recent_rounds_keep))

    @property
    def compact_threshold(self) -> float:
        return self._threshold

    @property
    def keep_tail(self) -> int:
        """循环内压缩保留的尾部消息条数（≈ 最近 N 轮）。"""
        return self._recent_rounds_keep * 4

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def should_compact_loop_messages(
        self, messages: list[dict[str, Any]], *, protected_head: int
    ) -> CompactionDecision:
        """判断循环内 messages 是否需要压缩。

        有 TokenCounter 时只看 Token 水位（与 Agent 层同一条线）；没有时才回落到
        消息条数的粗糙估算。
        """
        if self._counter is not None:
            if len(messages) <= protected_head + 4:
                return CompactionDecision(reason="too_few_messages")
            current = self._counter.count_messages(messages)
            limit = int(
                getattr(self._counter, "model_context_limit", _DEFAULT_CONTEXT_LIMIT)
                * self._threshold
            )
            if current > limit:
                return CompactionDecision(
                    should_compact=True,
                    reason="token_watermark",
                    current_tokens=current,
                    limit_tokens=limit,
                )
            return CompactionDecision(
                reason="below_watermark", current_tokens=current, limit_tokens=limit
            )

        # 无 TokenCounter：只能按条数兜底
        if len(messages) > protected_head + self.keep_tail + _NO_COUNTER_SLACK_MESSAGES:
            return CompactionDecision(should_compact=True, reason="message_count_fallback")
        return CompactionDecision(reason="below_watermark")

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _plan_loop_compaction(
        self, messages: list[dict[str, Any]], *, protected_head: int
    ) -> tuple[int, int] | None:
        """算出要压的区间 ``[keep_head, tail_start)``；无可压内容返回 None。"""
        keep_head = max(1, min(protected_head, len(messages)))
        tail_start = max(keep_head, len(messages) - self.keep_tail)
        # tool 消息必须紧跟它的 assistant tool_calls，不能把 assistant 切走留下孤儿 tool
        while tail_start > keep_head and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        if tail_start <= keep_head:
            return None
        return keep_head, tail_start

    def compact_loop_messages(
        self, messages: list[dict[str, Any]], *, protected_head: int
    ) -> bool:
        """就地压缩循环内 messages 的中间轮次（机械截断版）；返回是否真的压了。

        ``protected_head`` 是绝对不能动的头部消息条数（system + Checkpoint 锚点 +
        本轮用户问题），由 ``PromptLayoutOptimizer.initial_head_count`` 给出。
        """
        decision = self.should_compact_loop_messages(
            messages, protected_head=protected_head
        )
        if not decision.should_compact:
            return False
        span = self._plan_loop_compaction(messages, protected_head=protected_head)
        if span is None:
            return False
        keep_head, tail_start = span
        to_compact = messages[keep_head:tail_start]
        summary = self._summarize(to_compact)
        messages[keep_head:tail_start] = [{"role": "user", "content": summary}]
        self._log_compacted(decision, len(to_compact), keep_head, mode="truncate")
        return True

    async def compact_loop_messages_async(
        self,
        messages: list[dict[str, Any]],
        *,
        protected_head: int,
        summarize: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> bool:
        """同 :meth:`compact_loop_messages`，但优先用 LLM 做结构化摘要。

        机械截断把每条中间消息砍到 60 token，长任务里已经算好的结论、查到的
        artifact_id 会一起丢掉，模型只能重查。这里把待压区间转成可读的流水交给
        ``summarize``（通常是一次非流式 LLM 调用）；它失败、超时或返回空时，
        回落到机械截断，保证水位一定降下来。
        """
        decision = self.should_compact_loop_messages(
            messages, protected_head=protected_head
        )
        if not decision.should_compact:
            return False
        span = self._plan_loop_compaction(messages, protected_head=protected_head)
        if span is None:
            return False
        keep_head, tail_start = span
        to_compact = messages[keep_head:tail_start]

        summary_text: str | None = None
        if summarize is not None:
            try:
                summary_text = await summarize(self.render_for_summary(to_compact))
            except Exception as exc:  # noqa: BLE001
                logger.warning("react_loop_llm_compact_failed", error=str(exc))
                summary_text = None
        if summary_text and summary_text.strip():
            messages[keep_head:tail_start] = [
                {"role": "user", "content": REACT_COMPACT_MARK + "\n" + summary_text.strip()}
            ]
            self._log_compacted(decision, len(to_compact), keep_head, mode="llm")
            return True

        messages[keep_head:tail_start] = [
            {"role": "user", "content": self._summarize(to_compact)}
        ]
        self._log_compacted(decision, len(to_compact), keep_head, mode="truncate")
        return True

    def render_for_summary(
        self, to_compact: list[dict[str, Any]], *, per_message_tokens: int = 1500
    ) -> str:
        """把待压消息渲染成交给摘要模型的流水文本。

        每条保留的篇幅远大于机械截断（默认 1500 token），并完整保留工具调用
        的名字和参数——摘要模型要靠它们写出「查过什么」。
        """
        lines: list[str] = []
        for msg in to_compact:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            if REACT_COMPACT_MARK in content:
                lines.append("[已有摘要]\n" + content.replace(REACT_COMPACT_MARK, "").strip())
                continue
            if self._counter is not None:
                content = self._counter.truncate_to_tokens(content, per_message_tokens)
            else:
                content = content[: per_message_tokens * 2]
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = str(fn.get("arguments") or "")
                lines.append(f"[assistant 调用工具] {fn.get('name', '?')}({args[:600]})")
            if content.strip():
                lines.append(f"[{role}] {content}")
        return "\n\n".join(lines)

    @staticmethod
    def _log_compacted(
        decision: CompactionDecision, count: int, keep_head: int, *, mode: str
    ) -> None:
        logger.info(
            "react_loop_history_compacted",
            mode=mode,
            reason=decision.reason,
            current_tokens=decision.current_tokens,
            limit_tokens=decision.limit_tokens,
            compacted_messages=count,
            protected_head=keep_head,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _summarize(self, to_compact: list[dict[str, Any]]) -> str:
        """把中间轮次压成一条 user 摘要；已有摘要就地合并，不套娃。"""
        old_summary = ""
        summary_parts: list[str] = []
        for msg in to_compact:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = str(content)[:200]
            elif isinstance(content, str):
                if REACT_COMPACT_MARK in content:
                    old_summary = content.replace(REACT_COMPACT_MARK, "").strip()
                    continue
                if self._counter is not None:
                    content = self._counter.truncate_to_tokens(content, 60)
                else:
                    content = content[:200] + ("…" if len(content) > 200 else "")
            else:
                content = str(content)[:200]

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                tc_names = [
                    (tc.get("function") or {}).get("name", "?") for tc in tool_calls
                ]
                content = f"调用工具: {', '.join(tc_names)}"

            summary_parts.append(f"[{role}] {content}")

        merged: list[str] = []
        if old_summary:
            merged.append(old_summary[:800])
        merged.extend(summary_parts)
        return REACT_COMPACT_MARK + "\n" + "\n".join(merged)
