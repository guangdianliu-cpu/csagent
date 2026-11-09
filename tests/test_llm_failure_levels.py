"""LLM 失败日志的**分级**：谁该进 data/logs/error.log。

error.log 只收 ERROR，所以分级决定了事后能不能排查：

- 429 限流恒定 ERROR（事件名 ``llm_rate_limited``）——**即使这一次重试成功**。
  限流的典型表现是「答得慢」而不是「答不出」，只记最终失败会把它整个漏掉，
  正是当初 429 不好查的原因。
- 放弃重试（``return None``，用户拿到兜底话术）= ERROR。
- 还会再试一次的抖动 = warning，不许灌进 error.log。
"""

from __future__ import annotations

import logging

import httpx
import pytest
from unittest.mock import MagicMock

from csagent.infra.llm_client import LlmClient


def _settings(max_retries: int = 3) -> MagicMock:
    s = MagicMock()
    s.llm_api_key = "test-key"
    s.llm_base_url = "https://example.com/v1"
    s.llm_model = "test-model"
    s.llm_timeout_seconds = 30
    s.llm_max_retries = max_retries
    s.llm_temperature = 0.2
    s.llm_json_mode = True
    s.llm_log_bodies = False
    s.llm_log_bodies_dir = "data/llm_bodies"
    s.llm_max_tokens = 131072
    return s


class _Collector(logging.Handler):
    """自挂 root handler 收记录。

    不能用 pytest 的 caplog：``configure_logging`` 每次都会清空 root handlers
    （幂等性要求），把 caplog 自己的 handler 一并摘掉。这里在配置完成之后再挂，
    走的正是 error.log 那条 root handler 链路。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def log_records() -> list[logging.LogRecord]:
    """收集 (event 名, 级别)；structlog 走 stdlib，event dict 落在 record.msg。"""
    from csagent.infra.logging import configure_logging

    configure_logging("DEBUG", "console")  # 建立 structlog → stdlib 路由
    collector = _Collector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        yield collector.records
    finally:
        root.removeHandler(collector)


def _events(records: list[logging.LogRecord]) -> list[tuple[str, int]]:
    out = []
    for r in records:
        event = r.msg.get("event") if isinstance(r.msg, dict) else str(r.msg)
        out.append((event, r.levelno))
    return out


def test_rate_limit_logs_error_even_when_retry_will_continue(log_records: list):
    """429 且还会重试：仍然要有一条 ERROR 的 llm_rate_limited。"""
    client = LlmClient(_settings())
    client._log_http_failure(
        "llm_chat_failed",
        attempt=1,
        status=429,
        retryable=True,
        gave_up=False,
        error="429 Too Many Requests",
    )

    events = _events(log_records)
    assert ("llm_rate_limited", logging.ERROR) in events
    # 本次还会再试，所以结果事件本身只是 warning
    assert ("llm_chat_failed", logging.WARNING) in events


def test_rate_limit_that_gives_up_logs_both_at_error(log_records: list):
    client = LlmClient(_settings())
    client._log_http_failure(
        "llm_chat_failed",
        attempt=3,
        status=429,
        retryable=True,
        gave_up=True,
        error="429 Too Many Requests",
    )

    events = _events(log_records)
    assert ("llm_rate_limited", logging.ERROR) in events
    assert ("llm_chat_failed", logging.ERROR) in events


def test_giving_up_on_non_rate_limit_is_error(log_records: list):
    client = LlmClient(_settings())
    client._log_http_failure(
        "llm_chat_tools_failed",
        attempt=1,
        status=401,
        retryable=False,
        gave_up=True,
        error="401 Unauthorized",
    )
    assert ("llm_chat_tools_failed", logging.ERROR) in _events(log_records)


def test_transient_failure_stays_warning(log_records: list):
    """500 但还会再试：不该进 error.log，否则又变成流水账。"""
    client = LlmClient(_settings())
    client._log_http_failure(
        "llm_chat_failed",
        attempt=1,
        status=500,
        retryable=True,
        gave_up=False,
        error="500 Server Error",
    )
    levels = [lvl for _, lvl in _events(log_records)]
    assert logging.ERROR not in levels


@pytest.mark.asyncio
async def test_exhausted_retries_reach_error_end_to_end(
    log_records: list, monkeypatch: pytest.MonkeyPatch
):
    """端到端：连续 429 打满重试后，error.log 带上额度窗口和恢复时间。"""
    client = LlmClient(_settings(max_retries=2))

    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    body = (
        '{"error":{"code":"AccountQuotaExceeded","message":'
        '"You have exceeded the 5-hour usage quota. '
        'It will reset at 2026-09-23 14:00:00 +0800 CST. '
        'We recommend upgrading your plan.",'
        '"type":"TooManyRequests"}}'
    )

    async def _always_429(*args, **kwargs):
        response = httpx.Response(429, request=request, text=body)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    fake_client = MagicMock()
    fake_client.post = _always_429
    monkeypatch.setattr(client, "_ensure_client", lambda: fake_client)
    monkeypatch.setattr(client, "_backoff_seconds", lambda attempt, retry_after=None: 0)

    result = await client._post_with_retry(
        {"model": "test-model", "messages": []}, [], None, None
    )

    assert result is None
    events = _events(log_records)
    assert ("llm_rate_limited", logging.ERROR) in events
    assert ("llm_chat_failed", logging.ERROR) in events
    rate_limited = [
        r.msg
        for r in log_records
        if isinstance(r.msg, dict) and r.msg.get("event") == "llm_rate_limited"
    ]
    assert rate_limited
    assert rate_limited[0]["quota_window"] == "5-hour"
    assert rate_limited[0]["quota_reset_at"] == "2026-09-23 14:00:00 +0800 CST"
    assert "5小时额度已耗尽" in rate_limited[0]["quota_summary"]
