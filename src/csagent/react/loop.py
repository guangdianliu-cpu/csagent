"""ReAct 执行核心循环（Function Calling 版）— 状态机调度器。

重构自原 1266 行的单体 loop.py：
- 循环状态集中在 ReactContext（react/context.py）
- LLM 响应解析、工具执行、终答处理拆到 react/handlers.py
- 强制收口拆到 react/finalizer.py
- 旁白检测拆到 react/narration.py
- ReactLoop 退化为状态调度器 + 配置持有者
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from csagent.audit.trace_buffer import (
    append_audit_thought,
    append_audit_thought_event,
)
from csagent.infra.llm_client import LlmClient
from csagent.infra.logging import bind_context, reset_context
from csagent.memory.compaction_policy import HistoryCompactionPolicy
from csagent.prompts.domain_text import (
    FALLBACK_FAILED_ANSWER,
    FALLBACK_TIMEOUT_ANSWER,
    JSON_PREVIEW_KEYS,
)
from csagent.react.context import (
    DeltaCallback,
    ReactContext,
    ReactPhase,
    TerminationReason,
    ThoughtCallback,
    ThoughtEventCallback,
)
from csagent.react.finalizer import force_finalize
from csagent.react.handlers import (
    handle_plain_text,
    handle_tool_calls,
    parse_llm_response,
)
from csagent.react.models import DomainTaskRequest, DomainTaskResult
from csagent.react.budget import TurnBudget
from csagent.react.stall_guard import StallGuard
from csagent.react.thought_event import ThoughtEvent
from csagent.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# 单条工具结果在 prompt 中保留的最大字符数默认值
_DEFAULT_MAX_TOOL_RESULT_CHARS = 5000

# 软超时到点后不再开新一轮，而是用已有结果强制收口；硬超时 = 软超时 + 这段宽限，
# 只兜「收口本身也卡住」的极端情况。宽限要盖住一次收口 LLM 调用（含重试）。
HARD_TIMEOUT_GRACE_SECONDS = 240.0

# 循环内 LLM 摘要单次上限；超时即回落机械截断，不能让压缩把整轮拖死
_LOOP_COMPACT_TIMEOUT_SECONDS = 90.0

_LOOP_COMPACTOR_FALLBACK_PROMPT = (
    "你是任务执行过程的压缩器。把下面这段 Agent 在本轮任务中的执行流水压缩成 Markdown，"
    "分「任务目标 / 已完成的步骤与结论 / 产出物 / 下一步」四节。"
    "文件路径、artifact_id、代码、名称等标识不得丢失；本轮查到的关键数据可保留但需注明来源；"
    "若有「已有摘要」须合并进来。不要寒暄、不要复述原文。"
)


def _load_loop_compactor_prompt() -> str:
    from csagent.infra.config import CONFIGS_DIR

    try:
        path = CONFIGS_DIR / "prompts" / "loop_compactor.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return _LOOP_COMPACTOR_FALLBACK_PROMPT


_TOOL_RESULT_TRUNCATED_SUFFIX = (
    "\n…[展示已截断；工具已完整执行。"
    "大结果请 write_file/run_script 落盘后再 read_file，勿重复查询。]"
)


class ReactLoop:
    """领域 Agent 通用 ReAct 执行核心（状态机版）。

    职责仅限于：
    1. 初始化 ReactContext
    2. 按状态调度 Handler
    3. 管理循环终止条件
    """

    def __init__(
        self,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        max_invalid_replies: int = 3,
        stall_repeat_threshold: int = 3,
        max_tool_calls: int = 15,
        overall_timeout_seconds: float | None = 180.0,
        max_parallel_tool_calls: int = 5,
        max_tool_result_chars: int = _DEFAULT_MAX_TOOL_RESULT_CHARS,
        token_counter: Any = None,
        compact_threshold: float = 0.80,
        prompt_layout: Any = None,
        compaction_policy: HistoryCompactionPolicy | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self._max_invalid_replies = max(1, int(max_invalid_replies))
        self._stall_guard_factory = lambda: StallGuard(stall_repeat_threshold, max_tool_calls)
        self._overall_timeout_seconds = overall_timeout_seconds
        self._max_parallel_tool_calls = max(1, int(max_parallel_tool_calls))
        self._max_tool_result_chars = int(max_tool_result_chars)
        self._token_counter = token_counter
        if prompt_layout is not None:
            self._prompt_layout = prompt_layout
        else:
            from csagent.memory.prompt_layout import PromptLayoutOptimizer
            self._prompt_layout = PromptLayoutOptimizer(token_counter)
        # 压缩策略由装配层（main.py）构造一次、与 Agent 层共用同一实例；
        # 这里的兜底只为不注入策略的测试/脚本保留，参数与 Agent 层默认值一致。
        self._compaction_policy = compaction_policy or HistoryCompactionPolicy(
            token_counter, compact_threshold=compact_threshold
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run(
        self,
        request: DomainTaskRequest,
        system_prompt: str,
        agent_name: str,
        on_thought: ThoughtCallback | None = None,
        on_progress: ThoughtCallback | None = None,
        on_delta: DeltaCallback | None = None,
        on_reasoning: ThoughtCallback | None = None,
        on_thought_event: ThoughtEventCallback | None = None,
    ) -> DomainTaskResult:
        thought_trace: list[str] = []

        async def emit_event(event: ThoughtEvent) -> None:
            text = event.to_text()
            thought_trace.append(text)
            append_audit_thought(text)
            append_audit_thought_event(event.to_dict())
            if on_thought is not None:
                await on_thought(text)
            if on_thought_event is not None:
                await on_thought_event(event)

        try:
            if self._overall_timeout_seconds:
                # 正常路径靠 _state_machine 里的软截止收口；这里的硬超时只是保险丝
                return await asyncio.wait_for(
                    self._run_with_context(
                        request, system_prompt, agent_name,
                        thought_trace, emit_event,
                        on_progress, on_delta, on_reasoning,
                    ),
                    timeout=self._overall_timeout_seconds + HARD_TIMEOUT_GRACE_SECONDS,
                )
            return await self._run_with_context(
                request, system_prompt, agent_name,
                thought_trace, emit_event,
                on_progress, on_delta, on_reasoning,
            )
        except asyncio.TimeoutError:
            logger.error(
                "react_overall_timeout",
                task_id=request.task_id, domain=request.domain,
                timeout=self._overall_timeout_seconds,
            )
            await emit_event(ThoughtEvent(
                round_no=0, phase="timeout",
                result_preview="ReAct 循环超时，强制结束",
            ))
            return DomainTaskResult(
                task_id=request.task_id,
                domain=request.domain,
                status="timeout",
                answer_draft=FALLBACK_TIMEOUT_ANSWER,
                thought_trace=thought_trace,
                confidence=0.0,
            )

    # ------------------------------------------------------------------
    # 状态机主循环
    # ------------------------------------------------------------------

    async def _run_with_context(
        self,
        request: DomainTaskRequest,
        system_prompt: str,
        agent_name: str,
        thought_trace: list[str],
        emit_event: ThoughtEventCallback,
        on_progress: ThoughtCallback | None,
        on_delta: DeltaCallback | None = None,
        on_reasoning: ThoughtCallback | None = None,
    ) -> DomainTaskResult:
        ctx_tokens = bind_context(
            agent_name=agent_name,
            domain=request.domain,
            task_id=request.task_id,
        )
        try:
            return await self._state_machine(
                request, system_prompt, agent_name,
                thought_trace, emit_event,
                on_progress, on_delta, on_reasoning,
            )
        finally:
            reset_context(ctx_tokens)

    async def _state_machine(
        self,
        request: DomainTaskRequest,
        system_prompt: str,
        agent_name: str,
        thought_trace: list[str],
        emit_event: ThoughtEventCallback,
        on_progress: ThoughtCallback | None,
        on_delta: DeltaCallback | None,
        on_reasoning: ThoughtCallback | None,
    ) -> DomainTaskResult:
        # ---- 预算：开局写进系统提示词，循环中接近上限时再提醒（见 react/budget.py）----
        loop_clock = asyncio.get_running_loop()
        stall_guard = self._stall_guard_factory()
        budget = TurnBudget(
            max_rounds=request.max_rounds,
            max_tool_calls=stall_guard.max_tool_calls,
            time_limit_seconds=self._overall_timeout_seconds,
            started_at=loop_clock.time(),
        )
        system_prompt = f"{system_prompt.rstrip()}\n\n{budget.system_prompt_section()}"

        # ---- 初始化 ReactContext ----
        ctx = ReactContext(
            request=request,
            system_prompt=system_prompt,
            agent_name=agent_name,
            max_rounds=request.max_rounds,
            emit_event=emit_event,
            on_progress=on_progress,
            on_delta=on_delta,
            on_reasoning=on_reasoning,
            thought_trace=thought_trace,
            stall_guard=stall_guard,
        )

        # 初始化消息；同时记下头部保护区（system + Checkpoint 锚点 + 本轮问题），
        # 循环内压缩据此划界，不再靠嗅探 content 里有没有「Checkpoint」这个词
        ctx.messages = self._init_messages(system_prompt, request)
        ctx.protected_head = self._prompt_layout.initial_head_count(
            checkpoint_summary=request.checkpoint_summary or ""
        )

        # 构建工具 schema：只发业务工具，全轮全量，不再裁剪。
        ctx.tool_schemas = self._tool_registry.list_tool_schemas_for_agent(agent_name)

        ctx.phase = ReactPhase.LLM_CALL
        deadline = (
            loop_clock.time() + self._overall_timeout_seconds
            if self._overall_timeout_seconds
            else None
        )

        # ---- 状态机主循环 ----
        for round_no in range(1, request.max_rounds + 1):
            ctx.round_no = round_no

            if ctx.is_done:
                break

            # 软截止：到点不再开新一轮，用已拿到的工具结果收口。以前是整轮
            # wait_for 硬杀，已经查到的东西全部作废、只回一句兜底文案。
            if (
                deadline is not None
                and ctx.phase == ReactPhase.LLM_CALL
                and loop_clock.time() >= deadline
            ):
                logger.warning(
                    "react_soft_deadline_reached",
                    task_id=request.task_id,
                    rounds_used=round_no - 1,
                    timeout=self._overall_timeout_seconds,
                )
                await emit_event(ThoughtEvent(
                    round_no=round_no, phase="timeout",
                    result_preview="已到本轮时间上限，基于已有结果收口",
                ))
                ctx.termination_reason = TerminationReason.TIMEOUT
                ctx.phase = ReactPhase.FORCE_FINALIZE

            # ---- Phase: LLM_CALL ----
            if ctx.phase == ReactPhase.LLM_CALL:
                if on_progress is not None:
                    if round_no == 1:
                        await on_progress("正在分析您的问题…")
                    elif round_no > 2:
                        await on_progress("正在整理查询结果…")

                ctx.assistant_message = await self._llm_client.chat_with_tools(
                    ctx.messages, ctx.tool_schemas,
                    on_reasoning=on_reasoning,
                )
                if ctx.assistant_message is None:
                    logger.error("llm_unavailable", task_id=request.task_id)
                    ctx.termination_reason = TerminationReason.LLM_UNAVAILABLE
                    break

                # ---- Phase: PARSE_RESPONSE ----
                parse_llm_response(ctx)

                if ctx.content_text:
                    await emit_event(ThoughtEvent(
                        round_no=round_no, phase="llm_call",
                        result_preview=ctx.content_text,
                    ))

                # ---- Phase: 分派 ----
                if not ctx.tool_calls:
                    await handle_plain_text(
                        ctx, max_invalid_replies=self._max_invalid_replies
                    )
                else:
                    await handle_tool_calls(
                        ctx,
                        self._tool_registry,
                        max_parallel=self._max_parallel_tool_calls,
                        max_tool_result_chars=self._max_tool_result_chars,
                        truncate_tool_result_fn=self._truncate_tool_result,
                    )
                    if ctx.phase == ReactPhase.LLM_CALL:
                        self._append_budget_notice(ctx, budget, loop_clock.time())

                # 循环内历史压缩：纯水位兜底，策略与 Agent 层共用同一实例
                # （见 memory/compaction_policy.py 的模块注释）
                if ctx.phase == ReactPhase.LLM_CALL:
                    await self._compaction_policy.compact_loop_messages_async(
                        ctx.messages,
                        protected_head=ctx.protected_head,
                        summarize=self._summarize_loop_history,
                    )

            # ---- Phase: FORCE_FINALIZE ----
            if ctx.phase == ReactPhase.FORCE_FINALIZE:
                await force_finalize(ctx, self._llm_client)
                break

        # ---- LLM 不可用 / 超出轮次：兜底强制收口 ----
        if not ctx.is_done:
            if ctx.termination_reason is None:
                ctx.termination_reason = TerminationReason.MAX_ROUNDS
            await force_finalize(ctx, self._llm_client)

        logger.info(
            "react_loop_terminated",
            task_id=request.task_id,
            reason=(ctx.termination_reason.value if ctx.termination_reason else "unknown"),
            rounds_used=ctx.round_no,
            phase=ctx.phase.value,
        )

        if ctx.result is not None:
            return ctx.result

        return DomainTaskResult(
            task_id=request.task_id,
            domain=request.domain,
            status="failed",
            answer_draft=FALLBACK_FAILED_ANSWER,
            thought_trace=thought_trace,
            confidence=0.0,
        )

    @staticmethod
    def _append_budget_notice(ctx: ReactContext, budget: TurnBudget, now: float) -> None:
        """接近上限时把预算提醒附在本轮最后一条工具结果后面（每个级别只发一次）。"""
        last = ctx.messages[-1] if ctx.messages else None
        if not last or last.get("role") != "tool":
            return
        tool_calls = ctx.stall_guard.total_tool_calls if ctx.stall_guard else 0
        notice = budget.notice(ctx.round_no, tool_calls, now)
        if notice is None:
            return
        last["content"] = f"{last.get('content') or ''}\n\n{notice}"
        logger.info(
            "react_budget_notice",
            task_id=ctx.request.task_id,
            round=ctx.round_no,
            tool_calls=tool_calls,
            level=budget.sent_level.name.lower(),
        )

    async def _summarize_loop_history(self, transcript: str) -> str | None:
        """循环内压缩用的 LLM 摘要；失败/超时返回 None，由策略回落机械截断。"""
        try:
            return await asyncio.wait_for(
                self._llm_client.chat(
                    [
                        {"role": "system", "content": _load_loop_compactor_prompt()},
                        {"role": "user", "content": transcript},
                    ],
                    json_mode=False,
                    temperature=0.2,
                ),
                timeout=_LOOP_COMPACT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("react_loop_llm_compact_timeout")
            return None

    # ------------------------------------------------------------------
    # 消息初始化
    # ------------------------------------------------------------------

    def _init_messages(
        self, system_prompt: str, request: DomainTaskRequest
    ) -> list[dict[str, Any]]:
        return self._prompt_layout.assemble_initial_messages(
            system_prompt, request,
            persistent_instructions=request.persistent_instructions or "",
            checkpoint_summary=request.checkpoint_summary or "",
            dynamic_context=request.context_summary or "",
        )

    # ------------------------------------------------------------------
    # 工具结果截断
    # ------------------------------------------------------------------

    def _truncate_tool_result(self, tool_result: str) -> str:
        """截断过长的工具结果文本，避免 prompt 无界增长。

        这是外置失败时的**回落**（见 ``handlers._shrink_tool_result``）。
        正常路径会把全文冻到 artifacts，上下文只留结构摘要 + artifact_id；
        只有登记库不可用或写盘失败时才会走到这里，那时被切掉的尾巴确实拿不回来。

        只按 ``max_tool_result_chars`` 做字符硬顶；不再用 TokenCounter 二次腰斩
        （原先 ``limit // 2`` token），否则读长文时 3 万字预算名存实亡。
        循环整体水位仍由 HistoryCompactionPolicy 兜底。
        """
        limit = self._max_tool_result_chars
        if len(tool_result) <= limit:
            return tool_result

        stripped = tool_result.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
            if data is not None:
                summary = _summarize_json_tool_result(data)
                if summary and len(summary) <= limit:
                    return summary + _TOOL_RESULT_TRUNCATED_SUFFIX
        return tool_result[:limit] + _TOOL_RESULT_TRUNCATED_SUFFIX


# =====================================================================
# 模块级辅助函数（保持原有行为）
# =====================================================================

def _summarize_json_tool_result(data: Any) -> str | None:
    """把超大 JSON 工具结果压缩为 LLM 可读的摘要。"""
    if isinstance(data, dict):
        entity_key = next(
            (k for k in JSON_PREVIEW_KEYS if isinstance(data.get(k), dict)), None
        )
    else:
        entity_key = None
    if entity_key:
        entity_obj = data[entity_key]
        preview_keys = set(JSON_PREVIEW_KEYS.get(entity_key, ()))
        # 领域标识字段在 domain_text 的 json_preview_keys 里配；这里只兜底通用的 code/name
        fallback_ident_keys = ("code", "name")
        preview = {
            k: entity_obj[k]
            for k in entity_obj
            if k in preview_keys or k in fallback_ident_keys
        }
        summary = {
            "entity": data.get("entity"),
            "match": data.get("match"),
            f"{entity_key}_field_count": len(entity_obj),
            f"{entity_key}_preview": preview,
        }
        if data.get("candidates"):
            summary["candidates"] = data["candidates"]
        return json.dumps(summary, ensure_ascii=False, indent=2)

    if isinstance(data, dict):
        if len(data) <= 20:
            return json.dumps(data, ensure_ascii=False, indent=2)
        keys = list(data.keys())
        preview = {k: data[k] for k in keys[:12]}
        return json.dumps(
            {"_summary": f"共 {len(keys)} 个顶层字段", "preview": preview},
            ensure_ascii=False, indent=2,
        )

    if isinstance(data, list):
        return json.dumps(
            {"_summary": f"共 {len(data)} 条记录", "preview": data[:5]},
            ensure_ascii=False, indent=2,
        )

    return None
