"""ReAct 循环强制收口：当循环超轮次或停滞时，让 LLM 无 tools 输出终答。

从 loop.py 拆出的 _force_finalize / _finalize_with_stream 逻辑。
"""

from __future__ import annotations

import sys
from typing import Any

import structlog

from csagent.infra.llm_client import LlmClient
from csagent.prompts.domain_text import FALLBACK_FAILED_ANSWER
from csagent.react.context import (
    ReactContext,
    ReactPhase,
    TerminationReason,
    ThoughtCallback,
)
from csagent.react.models import DomainTaskResult
from csagent.react.narration import has_answer_substance, looks_like_interim_narration

logger = structlog.get_logger(__name__)

_FINAL_STREAM_PROMPT = (
    "请根据以上对话与工具结果，直接输出面向客户的最终回复正文。"
    "只输出回复内容，不要 JSON、不要 markdown 代码块、不要前缀标签。"
    "禁止输出「我先加载 / 正在查询 / 查看是否有」等过程旁白；"
    "若工具结果里已有名单或数字，据此给出结论。"
)

_FINAL_STREAM_RETRY_PROMPT = (
    "上一条仍是过程说明，不是对客终答。"
    "请基于已有工具结果直接给出面向客户的最终回复正文："
    "含具体结论/名单/数字；信息不足则如实说明已查到什么。"
    "禁止再说加载 Skill、正在查询、查看是否有字段。"
    "只输出回复内容，不要 JSON、不要代码块。"
)


def _chat(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def _is_interim_narration(text: str) -> bool:
    """纯旁白（无答案实质）才拦截；长文/表格/数字等实质内容 fail-open 放行。"""
    return bool(text) and looks_like_interim_narration(text) and not has_answer_substance(text)


def _truncate_log_text(text: str | None, *, limit: int = 200) -> str:
    raw = text or ""
    preview = raw if len(raw) <= limit else raw[:limit] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return preview.encode(encoding, errors="replace").decode(encoding, errors="replace")


async def force_finalize(
    ctx: ReactContext,
    llm_client: LlmClient,
) -> None:
    """强制收口：让 LLM 无 tools 输出终答，写入 ctx.result 并设 DONE。"""
    if ctx.on_delta is not None:
        await _finalize_with_stream(ctx, llm_client)
    else:
        await _finalize_batch(ctx, llm_client)

    if ctx.result is None:
        ctx.result = DomainTaskResult(
            task_id=ctx.request.task_id,
            domain=ctx.request.domain,
            status="failed",
            answer_draft=FALLBACK_FAILED_ANSWER,
            thought_trace=ctx.thought_trace,
            confidence=0.0,
        )
    ctx.phase = ReactPhase.DONE
    if ctx.termination_reason is None:
        ctx.termination_reason = TerminationReason.FORCE_FINALIZED


async def _finalize_batch(
    ctx: ReactContext,
    llm_client: LlmClient,
) -> None:
    """非流式强制收口。"""
    force_messages = list(ctx.messages)
    force_messages.append(_chat("user", _FINAL_STREAM_PROMPT))
    reply = await _safe_chat(llm_client, force_messages, on_reasoning=ctx.on_reasoning)
    text = (reply or "").strip()

    if text and _is_interim_narration(text):
        logger.warning(
            "react_force_finalize_interim_rejected",
            preview=_truncate_log_text(text),
        )
        force_messages.append(_chat("assistant", text))
        force_messages.append(_chat("user", _FINAL_STREAM_RETRY_PROMPT))
        retry = await _safe_chat(llm_client, force_messages, on_reasoning=ctx.on_reasoning)
        text = (retry or "").strip()
        if text and _is_interim_narration(text):
            text = ""

    if text:
        ctx.result = DomainTaskResult(
            task_id=ctx.request.task_id,
            domain=ctx.request.domain,
            status="success",
            answer_draft=text,
            structured_data={},
            confidence=0.7,
            thought_trace=ctx.thought_trace,
        )


async def _finalize_with_stream(
    ctx: ReactContext,
    llm_client: LlmClient,
) -> None:
    """流式强制收口：先完整生成并校验，确认非旁白后一次性推送。

    不能边生成边推：首轮若被判旁白，已推出的内容收不回来，
    用户会收到「旁白 + 重试正文」拼接的重复消息。
    """
    stream_messages = list(ctx.messages)
    stream_messages.append(_chat("user", _FINAL_STREAM_PROMPT))

    streamed = await llm_client.chat_stream(
        stream_messages, json_mode=False, temperature=0.3,
        on_delta=None, on_reasoning=ctx.on_reasoning,
    )
    final_text = (streamed or "").strip()

    if final_text and _is_interim_narration(final_text):
        logger.warning(
            "react_final_stream_interim_rejected",
            preview=_truncate_log_text(final_text),
        )
        retry_messages = list(stream_messages)
        retry_messages.append(_chat("assistant", final_text))
        retry_messages.append(_chat("user", _FINAL_STREAM_RETRY_PROMPT))
        retry = await llm_client.chat_stream(
            retry_messages, json_mode=False, temperature=0.3,
            on_delta=None, on_reasoning=ctx.on_reasoning,
        )
        final_text = (retry or "").strip()
        if final_text and _is_interim_narration(final_text):
            final_text = ""

    if not final_text:
        final_text = FALLBACK_FAILED_ANSWER
        logger.warning("react_final_stream_fallback", task_id=ctx.request.task_id)

    if ctx.on_delta is not None:
        await ctx.on_delta(final_text)

    ctx.result = DomainTaskResult(
        task_id=ctx.request.task_id,
        domain=ctx.request.domain,
        status="success" if final_text else "failed",
        answer_draft=final_text,
        thought_trace=ctx.thought_trace,
        confidence=0.7,
    )


async def _safe_chat(
    llm_client: LlmClient,
    messages: list[dict[str, Any]],
    *,
    on_reasoning: ThoughtCallback | None = None,
) -> str | None:
    if on_reasoning is not None:
        return await llm_client.chat_stream(messages, on_reasoning=on_reasoning)
    return await llm_client.chat(messages)
