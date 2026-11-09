"""LLM 调用封装，支持 OpenAI 兼容 API。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import random
import re
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from csagent.common.exceptions import LlmUnavailableError
from csagent.infra.config import PROJECT_ROOT, Settings
from csagent.infra.quota_alert import parse_provider_error

logger = structlog.get_logger(__name__)

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

# 5xx/429/网络超时视为可重试；4xx（鉴权、参数错误等）重试无意义，直接快速失败。
_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

# 限流单独成一类：即使这次重试成功了也要留痕，否则事后无从判断是不是被限流拖慢的。
_RATE_LIMIT_STATUS = 429

# 对端 Retry-After 的采信上限：超过这个值不如直接放弃，让用户拿兜底回复
_RETRY_AFTER_CAP_SECONDS = 30.0


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    """解析 Retry-After（秒数或 HTTP 日期）；没有或解析失败返回 None。"""
    if response is None:
        return None
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), _RETRY_AFTER_CAP_SECONDS))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        delta = parsedate_to_datetime(raw).timestamp() - time.time()
        return max(0.0, min(delta, _RETRY_AFTER_CAP_SECONDS))
    except (TypeError, ValueError):
        return None


@dataclass
class _StreamOutcome:
    """带 tools 的流式尝试结果。

    ``message`` 为 None 时由调用方回落非流式；``emitted_reasoning`` 说明思考已经推给
    前端了，回落时不能再推一遍（否则界面上出现两份思考）；``status`` / ``retry_after``
    让回落前按对端要求退避，而不是立刻再撞一次 429。
    """

    message: dict[str, Any] | None = None
    emitted_reasoning: bool = False
    status: int | None = None
    retry_after: float | None = None

_BODY_LOG_LOCK = threading.Lock()
_UNSAFE_FILENAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)
# 同一轮用户交互（同一 trace_id）复用一个时间戳文件
_TURN_FILE_BY_TRACE: dict[str, Path] = {}
_TURN_FILE_MAX = 256


def _normalize_llm_endpoint(base_url: str) -> tuple[str, str]:
    """拆分 base URL 与 chat/completions 路径，避免重复拼接。

    支持两种配置方式：
    - https://api.deepseek.com/v1
    - https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions
    """
    normalized = base_url.rstrip("/")
    if normalized.endswith(_CHAT_COMPLETIONS_SUFFIX):
        api_base = normalized[: -len(_CHAT_COMPLETIONS_SUFFIX)]
        return api_base, _CHAT_COMPLETIONS_SUFFIX
    return normalized, _CHAT_COMPLETIONS_SUFFIX


class LlmClient:
    """OpenAI 兼容 LLM 客户端，内置超时、状态码感知重试与抖动退避。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._api_base, self._chat_path = _normalize_llm_endpoint(settings.llm_base_url)

    def _ensure_client(self) -> httpx.AsyncClient:
        """延迟创建 HTTP 客户端，避免未配置 Key 时阻塞服务启动。"""
        if self._client is not None:
            return self._client
        if not self._settings.llm_api_key.strip():
            raise LlmUnavailableError(
                "未配置 LLM_API_KEY，请复制 .env.example 为 .env 并填入 API Key"
            )
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            # read 是「两次收到数据之间」的最长间隔：流式里模型一直在吐思考就不会断；
            # 非流式要等整段生成完，所以 llm.timeout_seconds 要按最长生成时间来配。
            timeout=httpx.Timeout(
                self._settings.llm_timeout_seconds, connect=10.0, write=30.0, pool=30.0
            ),
            headers={
                "Authorization": f"Bearer {self._settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            trust_env=False,
        )
        return self._client

    async def warmup(self) -> None:
        """启动时预建 HTTP 连接，消除首次请求的 TCP 握手延迟。"""
        if not self._settings.llm_api_key.strip():
            return
        try:
            self._ensure_client()
            # 发一个轻量请求触发连接建立；失败不阻塞启动
            await self._ensure_client().head(self._chat_path)
            logger.info("llm_client_warmed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_client_warmup_failed", error=str(exc))

    async def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        json_mode: bool | None = None,
        temperature: float | None = None,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        """非流式对话，失败时返回 None（内部已完成重试）。

        若提供 on_reasoning 且模型返回 reasoning_content，会在返回正文前回调一次完整思考文本。
        """
        payload = self._build_payload(
            messages, model=model, json_mode=json_mode, temperature=temperature
        )
        return await self._post_with_retry(
            payload, messages, json_mode, temperature, on_reasoning=on_reasoning
        )

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """带 function calling 的对话，返回完整 assistant message 对象。

        返回值包含 tool_calls（工具调用）或 content（纯文本回复）。
        失败时返回 None。
        """
        payload = self._build_payload(
            messages, json_mode=False, temperature=temperature, tools=tools
        )
        return await self._post_with_retry_tools(
            payload, messages, temperature, tools,
            on_reasoning=on_reasoning, on_delta=on_delta,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        json_mode: bool | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        """流式对话：content → on_delta，reasoning_content → on_reasoning；返回完整 content。

        失败时返回 None。非 2xx 时降级为非流式（chat）。

        :param model: 覆盖模型名（如视觉模型）；留空用配置里的主模型
        """
        payload = self._build_payload(
            messages, model=model, json_mode=json_mode, temperature=temperature
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        client = self._ensure_client()
        logger.debug(
            "llm_chat_stream_request",
            model=self._settings.llm_model,
            message_count=len(messages),
            json_mode=bool(self._settings.llm_json_mode if json_mode is None else json_mode),
        )
        self._log_request_body(payload, mode="stream")
        request_start = time.perf_counter()
        full_content: list[str] = []
        full_reasoning: list[str] = []
        usage = (0, 0)

        try:
            async with client.stream("POST", self._chat_path, json=payload) as response:
                if response.status_code != 200:
                    logger.warning(
                        "llm_chat_stream_non_200",
                        status=response.status_code,
                    )
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        await asyncio.sleep(
                            self._backoff_seconds(1, _retry_after_seconds(response))
                        )
                    return await self._post_with_retry(
                        self._build_payload(
                            messages,
                            model=model,
                            json_mode=json_mode,
                            temperature=temperature,
                        ),
                        messages,
                        json_mode,
                        temperature,
                        on_reasoning=on_reasoning,
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    piece_usage = extract_llm_usage(chunk)
                    if piece_usage != (0, 0):
                        usage = piece_usage
                    delta_obj = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    reasoning = _extract_reasoning_delta(delta_obj)
                    if reasoning:
                        full_reasoning.append(reasoning)
                        if on_reasoning is not None:
                            await on_reasoning(reasoning)
                    content_piece = delta_obj.get("content")
                    if content_piece:
                        full_content.append(content_piece)
                        if on_delta is not None:
                            await on_delta(content_piece)
        except httpx.HTTPError as exc:
            logger.warning("llm_chat_stream_failed", error=str(exc))
            return None

        content = "".join(full_content)
        if not content.strip():
            logger.warning("llm_chat_stream_empty")
            return None
        duration_ms = (time.perf_counter() - request_start) * 1000
        logger.debug(
            "llm_chat_stream_success",
            reply_length=len(content),
            reasoning_length=sum(len(x) for x in full_reasoning),
            duration_ms=round(duration_ms, 1),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
        )
        self._log_response_body(
            {
                "stream": True,
                "model": self._settings.llm_model,
                "usage": _usage_as_dict(usage),
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "".join(full_reasoning) or None,
                    }
                }],
            },
            mode="stream",
            duration_ms=duration_ms,
        )
        self._maybe_audit_llm_turn(
            mode="stream",
            messages=messages,
            content=content,
            duration_ms=duration_ms,
            usage=usage,
        )
        return content

    def _log_http_failure(
        self,
        event: str,
        *,
        attempt: int,
        status: int,
        retryable: bool,
        gave_up: bool,
        error: str,
        response_body: str | None = None,
    ) -> None:
        """按后果分级：中途重试是 warning，429 与「放弃」是 ERROR。

        分级的意义在于 ``data/logs/error.log`` 只收 ERROR —— 还会再试一次的
        抖动不该灌进去，但下面两类必须留下：

        - **429 限流**：无论这次是否重试成功都打 ERROR（事件名固定为
          ``llm_rate_limited``，便于 grep 与配告警）。限流往往表现为「答得慢」
          而不是「答不出」，只记最终失败会把它整个漏掉。
        - **放弃重试**：``return None`` 意味着用户这轮拿到的是兜底话术，
          属于真实故障。
        """
        fields: dict[str, Any] = {
            "attempt": attempt,
            "status": status,
            "retryable": retryable,
            "error": error,
        }
        # 429 / 最终失败都要把对端正文拆开展示。额度耗尽没有已用/总量，
        # 只有窗口和恢复时间；不依赖 LLM_LOG_BODIES，否则生产 error.log 看不到。
        interesting = status == _RATE_LIMIT_STATUS or gave_up
        parsed: dict[str, Any] = {}
        if interesting and response_body:
            parsed = parse_provider_error(response_body)
            fields.update(parsed)
            fields["response_body"] = response_body[:2000]

        if status == _RATE_LIMIT_STATUS:
            logger.error("llm_rate_limited", model=self._settings.llm_model, **fields)


        if gave_up:
            logger.error(event, gave_up=True, **fields)
        else:
            logger.warning(event, **fields)

    async def _post_with_retry(
        self,
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
        json_mode: bool | None,
        temperature: float | None,
        *,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        """非流式 POST + 状态码感知重试 + JSON mode 降级保护。"""
        client = self._ensure_client()
        max_retries = max(1, self._settings.llm_max_retries)
        logger.debug(
            "llm_chat_request",
            model=self._settings.llm_model,
            message_count=len(messages),
            json_mode=bool(self._settings.llm_json_mode if json_mode is None else json_mode),
        )
        self._log_request_body(payload, mode="chat")
        request_start = time.perf_counter()

        for attempt in range(1, max_retries + 1):
            retry_after: float | None = None
            try:
                response = await client.post(self._chat_path, json=payload)
                response.raise_for_status()
                data = response.json()
                message = data["choices"][0]["message"]
                content = message.get("content")
                if content is None or not content.strip():
                    raise _EmptyLlmContentError("LLM 返回空内容")
                reasoning = _extract_reasoning_message(message)
                if reasoning and on_reasoning is not None:
                    await on_reasoning(reasoning)
                duration_ms = (time.perf_counter() - request_start) * 1000
                usage = extract_llm_usage(data)
                logger.debug(
                    "llm_chat_success",
                    reply_length=len(content),
                    reasoning_length=len(reasoning or ""),
                    attempt=attempt,
                    duration_ms=round(duration_ms, 1),
                    prompt_tokens=usage[0],
                    completion_tokens=usage[1],
                )
                self._log_response_body(data, mode="chat", duration_ms=duration_ms, attempt=attempt)
                self._maybe_audit_llm_turn(
                    mode="chat",
                    messages=messages,
                    content=content,
                    duration_ms=duration_ms,
                    usage=usage,
                )
                return content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retry_after = _retry_after_seconds(exc.response)
                if status == 400 and "response_format" in payload:
                    logger.warning(
                        "llm_json_mode_unsupported_downgrade",
                        attempt=attempt,
                        status=status,
                    )
                    payload = self._build_payload(
                        messages, json_mode=False, temperature=temperature
                    )
                    self._log_request_body(payload, mode="chat_downgrade")
                    continue
                retryable = status in _RETRYABLE_STATUS_CODES
                error_body = ""
                try:
                    error_body = exc.response.text
                except Exception:  # noqa: BLE001
                    error_body = ""
                gave_up = not retryable or attempt >= max_retries
                self._log_http_failure(
                    "llm_chat_failed",
                    attempt=attempt,
                    status=status,
                    retryable=retryable,
                    gave_up=gave_up,
                    error=str(exc),
                    response_body=error_body or None,
                )
                if gave_up:
                    return None
            except (httpx.HTTPError, KeyError, IndexError, _EmptyLlmContentError) as exc:
                if attempt >= max_retries:
                    logger.error(
                        "llm_chat_failed", attempt=attempt, error=str(exc), gave_up=True
                    )
                    return None
                logger.warning("llm_chat_failed", attempt=attempt, error=str(exc))

            await asyncio.sleep(self._backoff_seconds(attempt, retry_after))
        return None

    async def _try_stream_tools(
        self,
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> _StreamOutcome:
        """带 tools 的流式调用；``message`` 为 None 时让调用方回落非流式。

        「没拿到可用结果」包含非 2xx、连接中断、以及既没有 content 也没有 tool_calls
        的空响应。回落走非流式重试，那条路有完整的 429/5xx 退避逻辑；这里只负责把
        「对端要求等多久」「思考是否已经推出去了」交代清楚。
        """
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        client = self._ensure_client()
        request_start = time.perf_counter()

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        usage = (0, 0)

        try:
            async with client.stream("POST", self._chat_path, json=stream_payload) as response:
                if response.status_code != 200:
                    # 429/5xx 交给非流式那条路重试，那里有退避与分级日志
                    logger.warning(
                        "llm_chat_tools_stream_non_200", status=response.status_code
                    )
                    return _StreamOutcome(
                        status=response.status_code,
                        retry_after=_retry_after_seconds(response),
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    piece_usage = extract_llm_usage(chunk)
                    if piece_usage != (0, 0):
                        usage = piece_usage
                    delta_obj = (chunk.get("choices") or [{}])[0].get("delta") or {}

                    reasoning = _extract_reasoning_delta(delta_obj)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        if on_reasoning is not None:
                            await on_reasoning(reasoning)

                    piece = delta_obj.get("content")
                    if piece:
                        content_parts.append(piece)
                        if on_delta is not None:
                            await on_delta(piece)

                    if delta_obj.get("tool_calls"):
                        _merge_tool_call_deltas(tool_acc, delta_obj)
        except httpx.HTTPError as exc:
            logger.warning(
                "llm_chat_tools_stream_failed",
                error=str(exc),
                emitted_reasoning=bool(reasoning_parts),
            )
            return _StreamOutcome(emitted_reasoning=bool(reasoning_parts))

        content = "".join(content_parts)
        tool_calls = _finish_tool_calls(tool_acc)
        if not content.strip() and not tool_calls:
            logger.warning("llm_chat_tools_stream_empty")
            return _StreamOutcome(emitted_reasoning=bool(reasoning_parts))

        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            message["tool_calls"] = tool_calls

        duration_ms = (time.perf_counter() - request_start) * 1000
        logger.debug(
            "llm_chat_tools_stream_success",
            has_tool_calls=bool(tool_calls),
            content_length=len(content),
            reasoning_length=sum(len(x) for x in reasoning_parts),
            duration_ms=round(duration_ms, 1),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
        )
        self._log_response_body(
            {"stream": True, "model": self._settings.llm_model,
             "usage": _usage_as_dict(usage),
             "choices": [{"message": message}]},
            mode="chat_tools_stream",
            duration_ms=duration_ms,
        )
        self._maybe_audit_llm_turn(
            mode="chat_tools_stream",
            messages=messages,
            content=content or str(tool_calls),
            duration_ms=duration_ms,
            usage=usage,
        )
        return _StreamOutcome(message=message, emitted_reasoning=bool(reasoning_parts))

    async def _post_with_retry_tools(
        self,
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
        temperature: float | None,
        tools: list[dict[str, Any]],
        *,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """带 tools 的 POST + 重试，返回完整 assistant message dict。

        走流式：``reasoning_content`` / ``content`` 逐片回调，``tool_calls`` 按
        index 累积后还原成非流式那套结构，对调用方完全透明。

        为什么要流式：这是主循环唯一的 LLM 调用，39/39 轮实测都从这里出答案。
        非流式时思考和正文都要等整段生成完才一次性到达，用户干等十几秒屏幕不动。

        任何一步出岔子（非 2xx、连接断、拼不出内容）都回落到原来的非流式重试，
        保证可用性不因为这个优化而下降。
        """
        client = self._ensure_client()
        max_retries = max(1, self._settings.llm_max_retries)
        logger.debug(
            "llm_chat_tools_request",
            model=self._settings.llm_model,
            message_count=len(messages),
            tool_count=len(tools),
        )
        self._log_request_body(payload, mode="chat_tools")
        request_start = time.perf_counter()

        streamed = await self._try_stream_tools(
            payload, messages, on_reasoning=on_reasoning, on_delta=on_delta
        )
        if streamed.message is not None:
            return streamed.message
        if streamed.emitted_reasoning:
            # 思考已经推给前端：回落调用再推一遍，界面上就会出现两份思考
            on_reasoning = None
        if streamed.status is not None and streamed.status in _RETRYABLE_STATUS_CODES:
            # 流式被 429/5xx 挡回：先按对端要求退避，再走非流式，别立刻再撞一次
            await asyncio.sleep(self._backoff_seconds(1, streamed.retry_after))

        for attempt in range(1, max_retries + 1):
            retry_after: float | None = None
            try:
                response = await client.post(self._chat_path, json=payload)
                response.raise_for_status()
                data = response.json()
                message = data["choices"][0]["message"]

                # 提取 reasoning（如果有）
                reasoning = _extract_reasoning_message(message)
                if reasoning and on_reasoning is not None:
                    await on_reasoning(reasoning)

                duration_ms = (time.perf_counter() - request_start) * 1000
                logger.debug(
                    "llm_chat_tools_success",
                    has_tool_calls=bool(message.get("tool_calls")),
                    content_length=len(message.get("content") or ""),
                    attempt=attempt,
                    duration_ms=round(duration_ms, 1),
                )
                self._log_response_body(data, mode="chat_tools", duration_ms=duration_ms, attempt=attempt)
                self._maybe_audit_llm_turn(
                    mode="chat_tools",
                    messages=messages,
                    content=message.get("content") or str(message.get("tool_calls")),
                    duration_ms=duration_ms,
                    usage=extract_llm_usage(data),
                )
                return message
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retry_after = _retry_after_seconds(exc.response)
                retryable = status in _RETRYABLE_STATUS_CODES
                gave_up = not retryable or attempt >= max_retries
                error_body = ""
                try:
                    error_body = exc.response.text
                except Exception:  # noqa: BLE001
                    error_body = ""
                self._log_http_failure(
                    "llm_chat_tools_failed",
                    attempt=attempt,
                    status=status,
                    retryable=retryable,
                    gave_up=gave_up,
                    error=str(exc),
                    response_body=error_body or None,
                )
                if gave_up:
                    return None
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                if attempt >= max_retries:
                    logger.error(
                        "llm_chat_tools_failed", attempt=attempt, error=str(exc), gave_up=True
                    )
                    return None
                logger.warning("llm_chat_tools_failed", attempt=attempt, error=str(exc))

            await asyncio.sleep(self._backoff_seconds(attempt, retry_after))
        return None

    def _maybe_audit_llm_turn(
        self,
        *,
        mode: str,
        messages: list[dict[str, Any]],
        content: str | None,
        duration_ms: float | None,
        usage: tuple[int, int] = (0, 0),
    ) -> None:
        """给审计缓冲记一次 LLM 调用：计数 + API 回报的 token 用量。

        不再把开关绑在 ``audit_log_enabled`` 上——用量是两个整数，缓冲始终存在
        （工具快照也靠它），关掉落盘不该连计数一起关掉。原文仍只在
        ``data/llm_bodies/``，审计 JSONL 只拿合计。
        """
        _ = mode, messages, content, duration_ms
        from csagent.audit.trace_buffer import get_audit_buffer

        buf = get_audit_buffer()
        if buf is None:
            return
        buf.note_llm_call(prompt_tokens=usage[0], completion_tokens=usage[1])

    def _note_body_log_path(self, path: Path) -> None:
        """把原文文件路径登记进本请求的审计缓冲，供 jsonl 摘要直链过去。

        走 contextvar 作用域的 buffer 而不是实例属性——LlmClient 是并发共享的，
        存在 self 上会串会话。
        """
        try:
            from csagent.audit.trace_buffer import get_audit_buffer

            buf = get_audit_buffer()
            if buf is None:
                return
            root = PROJECT_ROOT.resolve()
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            buf.set_llm_body_path(rel)
        except Exception:  # noqa: BLE001 - 审计辅助信息，绝不能影响主流程
            pass

    def _log_request_body(self, payload: dict[str, Any], *, mode: str) -> None:
        if not self._settings.llm_log_bodies:
            return
        path = self._append_body_record(payload, kind="request", mode=mode)
        logger.info(
            "llm_request_body",
            mode=mode,
            path=self._chat_path,
            message_count=len(payload.get("messages") or []),
            body_log=str(path) if path else None,
        )

    def _log_response_body(
        self,
        data: dict[str, Any],
        *,
        mode: str,
        duration_ms: float,
        attempt: int | None = None,
    ) -> None:
        if not self._settings.llm_log_bodies:
            return
        path = self._append_body_record(
            data, kind="response", mode=mode, duration_ms=duration_ms, attempt=attempt
        )
        logger.info(
            "llm_response_body",
            mode=mode,
            attempt=attempt,
            duration_ms=round(duration_ms, 1),
            body_log=str(path) if path else None,
        )

    def _body_log_dir(self) -> Path:
        raw = (getattr(self._settings, "llm_log_bodies_dir", None) or "data/llm_bodies").strip()
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def _append_body_record(
        self,
        data: Any,
        *,
        kind: str,
        mode: str = "",
        duration_ms: float | None = None,
        attempt: int | None = None,
    ) -> Path | None:
        """把一次 request/response 追加到「该用户这一轮交互」的 JSON 数组文件。

        目录按用户/群：``data/llm_bodies/{stem}/``；
        文件按本轮开始时间戳：``YYYYMMDD_HHMMSS_mmm.json``。
        同一 ``trace_id``（一次用户消息的 ReAct）写入同一个文件。
        """
        try:
            ctx = _llm_log_context()
            stem = conversation_log_stem(
                session_id=str(ctx.get("session_id") or ""),
                channel=str(ctx.get("channel") or ""),
            )
            now = datetime.now().astimezone()
            record: dict[str, Any] = {
                "ts": now.isoformat(timespec="milliseconds"),
                "kind": kind,
                "mode": mode,
                "session_id": ctx.get("session_id") or "",
                "channel": ctx.get("channel") or "",
                "trace_id": ctx.get("trace_id") or "",
                "body": data,
            }
            if duration_ms is not None:
                record["duration_ms"] = round(duration_ms, 1)
            if attempt is not None:
                record["attempt"] = attempt

            root = self._body_log_dir()
            with _BODY_LOG_LOCK:
                out = resolve_turn_body_log_path(
                    root,
                    stem=stem,
                    trace_id=str(ctx.get("trace_id") or ""),
                    when=now,
                )
                append_json_array_object(out, record)
            self._note_body_log_path(out)
            return out
        except OSError as exc:
            logger.warning("llm_body_log_write_failed", error=str(exc))
            return None

    @staticmethod
    def _backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
        """指数退避 + 抖动；对端给了 Retry-After 就按它来（有上限）。"""
        if retry_after is not None:
            return retry_after + random.uniform(0, 0.3)
        base = 0.5 * (2 ** (attempt - 1))
        jitter = random.uniform(0, 0.3)
        return min(base + jitter, 8.0)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        json_mode: bool | None,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self._settings.llm_model,
            "messages": messages,
            "stream": False,
            "temperature": (
                self._settings.llm_temperature if temperature is None else temperature
            ),
        }
        # 显式带上限，避免方舟不传时默认 4096 把长终答截断。
        max_tokens = _positive_int(getattr(self._settings, "llm_max_tokens", 0))
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            # 使用 function calling 时，不设置 json_mode（两者互斥）
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        else:
            use_json_mode = self._settings.llm_json_mode if json_mode is None else json_mode
            has_multimodal = any(
                isinstance(m.get("content"), list)
                and any(
                    isinstance(p, dict) and p.get("type") in {"image_url", "image"}
                    for p in m["content"]
                )
                for m in messages
                if isinstance(m, dict)
            )
            if use_json_mode and not has_multimodal:
                payload["response_format"] = {"type": "json_object"}
        return payload


def conversation_log_stem(*, session_id: str = "", channel: str = "") -> str:
    """一个用户或一个群聊对应一个日志目录名（不含扩展名）。

    企微群：wecom:group:{chatid} → wecom_group_{chatid}（整群共用）。
    企微单聊：wecom:{chattype}:{userid} → wecom_{chattype}_{userid}。
    其它渠道：直接用 session_id（web 端形如 web:{userId}）。
    """
    sid = (session_id or "").strip()
    parts = sid.split(":")
    if len(parts) >= 3 and parts[0].lower() == "wecom" and parts[1].lower() == "group":
        return _safe_filename(f"wecom_group_{parts[2]}")
    if len(parts) >= 3 and parts[0].lower() == "wecom":
        return _safe_filename(f"wecom_{parts[1]}_{parts[2]}")
    if sid:
        return _safe_filename(sid)
    return "unknown"


def turn_log_stamp(when: datetime) -> str:
    """一轮交互文件名中的时间戳：``20260816_033419_132``。"""
    return when.strftime("%Y%m%d_%H%M%S_") + f"{when.microsecond // 1000:03d}"


def extract_llm_usage(payload: Any) -> tuple[int, int]:
    """从 OpenAI 兼容响应里取出 ``(prompt_tokens, completion_tokens)``。

    缺字段、类型不对、流式中间片没有 usage，一律返回 ``(0, 0)``。
    调用方用「非零则覆盖」处理流式末片——那边给的是累计值，不是增量。
    """
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return _nonneg_int(usage.get("prompt_tokens")), _nonneg_int(
        usage.get("completion_tokens")
    )


def _usage_as_dict(usage: tuple[int, int]) -> dict[str, int] | None:
    """把用量写进合成的流式响应体；全零则省略，避免假装对端报了 0。"""
    if usage == (0, 0):
        return None
    return {"prompt_tokens": usage[0], "completion_tokens": usage[1]}


def _nonneg_int(value: Any) -> int:
    """usage 字段转非负整数；非法当 0。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _positive_int(value: Any) -> int:
    """配置值转正整数；非法或 <=0 视为未配置。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def parse_record_timestamp(ts: str) -> datetime | None:
    """解析日志记录里的 ISO 时间戳。"""
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def resolve_turn_body_log_path(
    root: Path,
    *,
    stem: str,
    trace_id: str = "",
    when: datetime | None = None,
) -> Path:
    """解析本轮交互应写入的文件路径；同一 trace 复用首次分配的时间戳文件。"""
    folder = root / (stem or "unknown")
    folder.mkdir(parents=True, exist_ok=True)
    trace = (trace_id or "").strip()
    if trace and trace in _TURN_FILE_BY_TRACE:
        return _TURN_FILE_BY_TRACE[trace]

    stamp = turn_log_stamp(when or datetime.now().astimezone())
    path = folder / f"{stamp}.json"
    if path.exists():
        suffix = (trace[:8] if trace else "x")
        path = folder / f"{stamp}_{suffix}.json"
    if trace:
        _TURN_FILE_BY_TRACE[trace] = path
        while len(_TURN_FILE_BY_TRACE) > _TURN_FILE_MAX:
            _TURN_FILE_BY_TRACE.pop(next(iter(_TURN_FILE_BY_TRACE)))
    return path


def split_legacy_session_body_log(legacy_path: Path) -> list[Path]:
    """把旧版「一用户一个 JSON 数组」拆成「一轮一个时间戳文件」。

    按 ``trace_id`` 分组；文件名取该轮第一条记录的 ``ts``。
    拆完后把原文件改名为 ``*.legacy.json``，避免和目录重名。
    """
    if not legacy_path.is_file():
        return []
    raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else [raw]
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("trace_id") or "").strip() or f"nots_{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)

    stem = legacy_path.stem
    root = legacy_path.parent
    written: list[Path] = []
    used_names: set[str] = set()
    for key in order:
        items = groups[key]
        first_ts = parse_record_timestamp(str(items[0].get("ts") or ""))
        when = first_ts or datetime.now().astimezone()
        stamp = turn_log_stamp(when)
        name = f"{stamp}.json"
        if name in used_names:
            name = f"{stamp}_{key[:8] or 'x'}.json"
        used_names.add(name)
        out = root / stem / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_dumps_body(items) + "\n", encoding="utf-8")
        written.append(out)
        if key:
            with _BODY_LOG_LOCK:
                _TURN_FILE_BY_TRACE[key] = out

    bak = legacy_path.with_name(legacy_path.stem + ".legacy.json")
    if bak.exists():
        bak = legacy_path.with_name(
            f"{legacy_path.stem}.legacy.{turn_log_stamp(datetime.now().astimezone())}.json"
        )
    legacy_path.replace(bak)
    return written


def _safe_filename(raw: str) -> str:
    name = _UNSAFE_FILENAME_RE.sub("_", raw).strip("._") or "unknown"
    return name[:120]


def _llm_log_context() -> dict[str, Any]:
    """读取当前请求绑定的 session / customer / channel / trace。"""
    try:
        from structlog.contextvars import get_contextvars

        return dict(get_contextvars() or {})
    except Exception:  # noqa: BLE001
        return {}


def append_json_array_object(path: Path, obj: Any) -> None:
    """把一个 JSON 对象追加到顶层数组文件，保持 indent=2 便于折叠。"""
    item = _dumps_body(obj)
    indented = "\n".join("  " + line if line else line for line in item.splitlines())
    if not path.exists() or path.stat().st_size == 0:
        path.write_text("[\n" + indented + "\n]\n", encoding="utf-8")
        return

    text = path.read_text(encoding="utf-8").rstrip()
    if not text.endswith("]"):
        # 旧版单对象文件：包进数组再追加
        try:
            old = json.loads(text)
        except json.JSONDecodeError:
            path.write_text("[\n" + indented + "\n]\n", encoding="utf-8")
            return
        merged = old if isinstance(old, list) else [old]
        merged.append(obj)
        path.write_text(_dumps_body(merged) + "\n", encoding="utf-8")
        return

    inner = text[:-1].rstrip()
    if inner.endswith("["):
        path.write_text("[\n" + indented + "\n]\n", encoding="utf-8")
        return
    path.write_text(inner + ",\n" + indented + "\n]\n", encoding="utf-8")


def _dumps_body(data: Any) -> str:
    """序列化请求/响应 body：缩进 + 中文可读。"""
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        return str(data)


class _EmptyLlmContentError(Exception):
    """LLM 返回了空字符串内容，视为可重试的失败。"""


def _merge_tool_call_deltas(
    acc: dict[int, dict[str, Any]], delta_obj: dict[str, Any]
) -> None:
    """把一帧 SSE 里的 tool_calls 分片并进累积表（按 index 归位）。

    OpenAI 兼容协议下工具调用是拆开下发的：第一帧带 id 和 function.name，
    后续帧只带 arguments 的片段，且**只能靠 index 认领**——

        {"index":0,"id":"call_x","function":{"name":"run_script","arguments":"{\"scr"}}
        {"index":0,"function":{"arguments":"ipt\":\"query.py\"}"}}

    arguments 必须按到达顺序拼成完整 JSON 串再解析，中途解析必然失败。
    index 缺省时按 0 处理：有的实现单工具调用时不带这个字段。
    """
    for frag in delta_obj.get("tool_calls") or []:
        if not isinstance(frag, dict):
            continue
        idx = frag.get("index")
        idx = int(idx) if isinstance(idx, int) else 0
        slot = acc.setdefault(
            idx,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if frag.get("id"):
            slot["id"] = frag["id"]
        if frag.get("type"):
            slot["type"] = frag["type"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            # name 一般只来一次；万一分片就接上，别覆盖成后半截
            slot["function"]["name"] += fn["name"] if slot["function"]["name"] == "" else ""
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def _finish_tool_calls(acc: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """累积表 → 按 index 升序的 tool_calls 列表；丢掉没名字的空槽。"""
    return [acc[i] for i in sorted(acc) if acc[i]["function"]["name"]]


def _extract_reasoning_delta(delta_obj: dict[str, Any]) -> str | None:
    """从流式 delta 提取思考增量（兼容 DeepSeek / 部分网关字段名）。"""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta_obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_reasoning_message(message: dict[str, Any]) -> str | None:
    """从非流式 message 提取完整思考文本。"""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
