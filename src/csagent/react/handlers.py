"""ReAct 循环的 LLM 响应处理器：将旧 _react_loop_body 中的「解析 + 分派」逻辑拆出。

每个 Handler 是一个无状态函数（或轻量类），接收 ReactContext、修改 context.phase 并
写入结果，由 ReactLoop 主循环调度。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import structlog

from csagent.audit.trace_buffer import (
    ToolCallRecord,
    append_audit_tool_call,
)
from csagent.memory.artifact_offload import offload_tool_result
from csagent.react.context import ReactContext, ReactPhase, TerminationReason
from csagent.react.models import (
    ActionType,
    AgentAction,
    DomainTaskRequest,
    DomainTaskResult,
)
from csagent.react.narration import has_answer_substance, looks_like_interim_narration
from csagent.react.thought_event import ThoughtEvent, truncate_preview
from csagent.tools.registry import ToolRegistry
from csagent.user_progress import progress_for_tool

logger = structlog.get_logger(__name__)

# ---- 提示词常量 ----
PLAIN_TEXT_RETRY_PROMPT = (
    "上一条不是最终答复：请调用业务工具收集信息，"
    "或直接输出面向客户的完整回复正文。"
    "不要只输出「我来查一下 / 我先加载 Skill」之类的中间说明。"
)


# =====================================================================
# 响应解析：从 assistant_message 提取 tool_calls / content
# =====================================================================

def parse_llm_response(ctx: ReactContext) -> None:
    """解析本轮 LLM 响应，更新 ctx.tool_calls / ctx.content_text。"""
    msg = ctx.assistant_message
    if msg is None:
        ctx.tool_calls = []
        ctx.content_text = ""
        return
    ctx.tool_calls = msg.get("tool_calls") or []
    content = msg.get("content") or ""
    ctx.content_text = content.strip() if isinstance(content, str) else str(content).strip()


# =====================================================================
# 纯文本响应处理（无 tool_calls）
# =====================================================================

async def handle_plain_text(ctx: ReactContext, *, max_invalid_replies: int = 3) -> None:
    """处理 LLM 返回纯文本（无 tool_calls）的情况。

    合格终答 → DONE；旁白但含实质内容（长文/表格/数字）→ fail-open 放行；
    纯旁白或空文本 → 重试或强制收口。
    """
    text = ctx.content_text

    if text and not looks_like_interim_narration(text):
        # 合格终答
        await _deliver_plain_text(ctx, text)
        return

    if text and has_answer_substance(text):
        # fail-open：旁白特征命中但答案实质明确，放行避免误杀完整终答
        logger.info(
            "react_plain_text_fail_open",
            round=ctx.round_no,
            chars=len(text),
            preview=_truncate_log_text(text),
        )
        await _deliver_plain_text(ctx, text)
        return

    # 纯旁白或空文本
    ctx.consecutive_invalid += 1
    logger.info(
        "react_plain_text_rejected",
        round=ctx.round_no,
        consecutive_invalid=ctx.consecutive_invalid,
        preview=_truncate_log_text(text),
    )
    if ctx.consecutive_invalid >= max_invalid_replies or not text:
        ctx.phase = ReactPhase.FORCE_FINALIZE
        ctx.termination_reason = TerminationReason.MAX_INVALID
        return

    # 追加重试提示，继续循环
    ctx.messages.append(_normalize_assistant_message(ctx.assistant_message))
    ctx.messages.append(_chat("user", PLAIN_TEXT_RETRY_PROMPT))
    ctx.phase = ReactPhase.LLM_CALL  # 回到 LLM 调用


async def _deliver_plain_text(ctx: ReactContext, text: str) -> None:
    """把纯文本作为合格终答交付（合格直发与 fail-open 放行共用）。"""
    ctx.consecutive_invalid = 0
    if ctx.on_delta is not None:
        await ctx.on_delta(text)
    ctx.result = _to_final_result_from_text(ctx.request, text, ctx.thought_trace)
    ctx.termination_reason = TerminationReason.PLAIN_TEXT_ANSWER
    ctx.phase = ReactPhase.DONE


# =====================================================================
# 工具调用处理
# =====================================================================

async def handle_tool_calls(
    ctx: ReactContext,
    tool_registry: ToolRegistry,
    max_parallel: int,
    max_tool_result_chars: int,
    truncate_tool_result_fn,
) -> None:
    """处理 LLM 返回的 tool_calls 批次：业务工具按批并行执行。

    所有 tool_calls 均为业务工具（系统收口工具已彻底下线，不再广告也不再拦截）。
    模型若幻觉调用未知工具名，ToolRegistry 返回权限错误文本回灌，下一轮自行纠正。
    """
    ctx.consecutive_invalid = 0
    ctx.messages.append(_normalize_assistant_message(ctx.assistant_message))

    answered_ids: set[str] = set()
    pending_batch: list[tuple[str, str, dict[str, Any]]] = []

    async def flush_batch() -> None:
        if not pending_batch:
            return
        await _execute_tool_batch(
            pending_batch, ctx, tool_registry,
            max_tool_result_chars=max_tool_result_chars,
            truncate_fn=truncate_tool_result_fn,
            answered_ids=answered_ids,
        )
        pending_batch.clear()

    for tc in ctx.tool_calls:
        func = tc.get("function") or {}
        tool_name = str(func.get("name") or "")
        tool_call_id = str(tc.get("id") or "")
        raw_args = func.get("arguments") or "{}"

        # 解析参数 JSON；失败则回写 tool error，不静默空跑
        arguments, parse_error = _parse_tool_arguments(raw_args)
        if arguments is None:
            _append_tool_result(
                ctx.messages, answered_ids, tool_call_id,
                f"错误: {parse_error}",
            )
            _record_rejected_call(ctx.round_no, tool_name, raw_args, parse_error or "")
            continue

        # ---- 业务工具：停滞检测 + 批次执行 ----
        call_action = AgentAction(
            action=ActionType.CALL_TOOL,
            tool=tool_name,
            arguments=arguments,
        )
        if ctx.stall_guard is not None and ctx.stall_guard.is_stalled(call_action):
            await flush_batch()
            logger.warning(
                "react_stalled",
                round=ctx.round_no,
                total_tool_calls=ctx.stall_guard.total_tool_calls,
                reason=(
                    ctx.stall_guard.last_reason.value
                    if ctx.stall_guard.last_reason else None
                ),
            )
            await ctx.emit_event(ThoughtEvent(
                round_no=ctx.round_no, phase="stall",
                tool_name=tool_name,
                result_preview=ctx.stall_guard.preview_message(),
            ))
            _record_rejected_call(
                ctx.round_no, tool_name, arguments,
                ctx.stall_guard.reject_message(),
            )
            _fill_unanswered_tool_calls(
                ctx.messages, ctx.tool_calls, answered_ids,
                ctx.stall_guard.cancel_message(),
                round_no=ctx.round_no,
            )
            ctx.termination_reason = TerminationReason.STALL
            ctx.phase = ReactPhase.FORCE_FINALIZE
            return

        pending_batch.append((tool_call_id, tool_name, arguments))
        if len(pending_batch) >= max_parallel:
            await flush_batch()

    # 执行剩余
    await flush_batch()

    # 继续下一轮
    if ctx.phase != ReactPhase.DONE:
        ctx.phase = ReactPhase.LLM_CALL


# =====================================================================
# 工具批次执行
# =====================================================================

async def _execute_tool_batch(
    batch: list[tuple[str, str, dict[str, Any]]],
    ctx: ReactContext,
    tool_registry: ToolRegistry,
    *,
    max_tool_result_chars: int,
    truncate_fn,
    answered_ids: set[str],
) -> None:
    """并行执行一批业务工具调用，按原始顺序回写结果。"""
    # 先发射所有 tool_exec 事件
    for _tool_call_id, tool_name, arguments in batch:
        logger.info(
            "工具调用", round=ctx.round_no, tool=tool_name,
            arguments=_truncate_arguments(arguments),
        )
        await ctx.emit_event(ThoughtEvent(
            round_no=ctx.round_no, phase="tool_exec",
            tool_name=tool_name,
            arguments_preview=truncate_preview(_truncate_arguments(arguments), 200),
        ))
        if ctx.on_progress is not None:
            progress_msg = progress_for_tool(tool_name, arguments)
            if progress_msg:
                await ctx.on_progress(progress_msg)

    async def run_one(tool_name: str, arguments: dict[str, Any]):
        """返回 (结果文本, 耗时 ms, 异常文本, 判定)。注册中心抛异常时判定为 None。"""
        start = time.monotonic()
        try:
            outcome = await tool_registry.execute_detailed(
                tool_name, arguments, ctx.request.customer_scope, agent_name=ctx.agent_name,
            )
            return outcome.text, int((time.monotonic() - start) * 1000), None, outcome
        except Exception as exc:  # noqa: BLE001
            return (
                f"工具 {tool_name} 执行异常: {exc}",
                int((time.monotonic() - start) * 1000),
                str(exc),
                None,
            )

    results = await asyncio.gather(
        *(run_one(name, args) for _cid, name, args in batch)
    )

    for (tool_call_id, tool_name, arguments), (tool_result, duration_ms, error, outcome) in zip(
        batch, results
    ):
        logger.info(
            "工具调用结果",
            round=ctx.round_no, tool=tool_name,
            result_chars=len(tool_result or ""),
            result_preview=_truncate_log_text(tool_result),
        )
        append_audit_tool_call(ToolCallRecord(
            round_no=ctx.round_no,
            tool_name=tool_name,
            arguments_preview=truncate_preview(_truncate_arguments(arguments), 200) or "",
            result_preview=truncate_preview(tool_result, 500) or "",
            duration_ms=duration_ms,
            # 拒绝 / 待确认如实记录，不再一律 success
            status="error" if error else (outcome.status if outcome is not None else "success"),
            risk=outcome.risk if outcome is not None else "",
            decision=outcome.decision if outcome is not None else "",
            rule_id=outcome.rule_id if outcome is not None else "",
        ))
        await ctx.emit_event(ThoughtEvent(
            round_no=ctx.round_no, phase="tool_result",
            tool_name=tool_name,
            result_preview=tool_result,
            duration_ms=duration_ms,
            error=error,
        ))
        _append_tool_result(
            ctx.messages, answered_ids, tool_call_id,
            _shrink_tool_result(
                tool_result,
                ctx=ctx,
                tool_name=tool_name,
                limit=max_tool_result_chars,
                truncate_fn=truncate_fn,
            ),
        )


def _shrink_tool_result(
    tool_result: str,
    *,
    ctx: ReactContext,
    tool_name: str,
    limit: int,
    truncate_fn,
) -> str:
    """把超长工具结果压进上下文预算：先试外置，不行才硬截断。

    两者的差别是**信息降级还是信息丢失**：外置把全文冻结到磁盘、上下文里留结构
    摘要加 artifact_id，模型随时能取回；硬截断切掉的尾巴再也没有了，而且模型看不出
    自己少了什么，会拿着残缺数据继续答。所以外置是首选，截断只是兜底。

    :param ctx: 当前 ReAct 上下文，用于取 session_id
    :param tool_name: 产生该结果的工具名，写进占位文本
    :param limit: 单条结果进上下文的字符上限
    :param truncate_fn: 外置不可用时的硬截断回落
    :returns: 可直接回灌进 messages 的文本
    """
    if not tool_result or len(tool_result) <= limit:
        return tool_result or ""

    scope = getattr(ctx.request, "customer_scope", None)
    session_id = str(getattr(scope, "session_id", "") or "")
    try:
        placeholder = offload_tool_result(
            tool_result, session_id=session_id, tool_name=tool_name, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        # 外置是增强不是依赖：磁盘、权限、登记库出任何问题都只能降级，
        # 不能让一次已经成功执行完的工具调用变成失败。
        logger.warning("tool_result_offload_failed", tool=tool_name, error=str(exc))
        placeholder = None

    return placeholder if placeholder is not None else truncate_fn(tool_result)


# =====================================================================
# 辅助函数（从 loop.py 迁移，保持行为不变）
# =====================================================================

def _parse_tool_arguments(raw_args) -> tuple[dict[str, Any] | None, str | None]:
    """解析工具调用参数 JSON。

    返回 ``(arguments, error)``：成功时 error 为 None；失败时 arguments 为 None，
    error 是给模型看的修正提示（三类错误分开，便于模型定位问题）。
    """
    if isinstance(raw_args, dict):
        return raw_args, None
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            return None, "arguments 不是合法 JSON，请修正后重试该工具调用。"
        if not isinstance(parsed, dict):
            return None, "arguments 必须是 JSON 对象。"
        return parsed, None
    return None, "arguments 类型无效，请传 JSON 对象字符串。"


def _append_tool_result(
    messages: list[dict[str, Any]],
    answered_ids: set[str],
    tool_call_id: str,
    content: str,
) -> None:
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    })
    if tool_call_id:
        answered_ids.add(tool_call_id)


def _fill_unanswered_tool_calls(
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    answered_ids: set[str],
    reason: str,
    *,
    round_no: int = 0,
) -> None:
    for tc in tool_calls:
        call_id = str(tc.get("id") or "")
        func = tc.get("function") or {}
        tool_name = str(func.get("name") or "")
        if call_id and call_id in answered_ids:
            continue
        _append_tool_result(messages, answered_ids, call_id, reason)
        if tool_name:
            append_audit_tool_call(ToolCallRecord(
                round_no=round_no,
                tool_name=tool_name,
                arguments_preview=_truncate_arguments_raw(func.get("arguments")),
                result_preview=reason,
                duration_ms=0,
                status="cancelled",
            ))


def _record_rejected_call(round_no: int, tool_name: str, arguments, reason: str) -> None:
    if not tool_name:
        return
    preview = arguments if isinstance(arguments, str) else _truncate_arguments(arguments)
    append_audit_tool_call(ToolCallRecord(
        round_no=round_no,
        tool_name=tool_name,
        arguments_preview=truncate_preview(str(preview), 200) or "",
        result_preview=reason,
        duration_ms=0,
        status="rejected",
    ))


def _normalize_assistant_message(msg: dict[str, Any] | None) -> dict[str, Any]:
    if msg is None:
        return {"role": "assistant", "content": None}
    normalized: dict[str, Any] = {"role": "assistant"}
    normalized["content"] = msg.get("content") or None
    if msg.get("tool_calls"):
        normalized["tool_calls"] = msg["tool_calls"]
    return normalized


def _chat(role: str, content: str | list) -> dict[str, Any]:
    return {"role": role, "content": content}


def _to_final_result_from_text(
    request: DomainTaskRequest,
    text: str,
    thought_trace: list[str],
) -> DomainTaskResult:
    return DomainTaskResult(
        task_id=request.task_id,
        domain=request.domain,
        status="success",
        answer_draft=text,
        structured_data={},
        confidence=0.7,
        thought_trace=thought_trace,
    )


def _truncate_arguments(arguments: dict[str, Any], *, limit: int = 500) -> str:
    text = json.dumps(arguments, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _truncate_arguments_raw(raw_args, *, limit: int = 500) -> str:
    text = raw_args if isinstance(raw_args, str) else str(raw_args)
    return text if len(text) <= limit else text[:limit] + "…"


def _truncate_log_text(text: str | None, *, limit: int = 200) -> str:
    raw = text or ""
    preview = raw if len(raw) <= limit else raw[:limit] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return preview.encode(encoding, errors="replace").decode(encoding, errors="replace")
