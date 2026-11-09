"""解析方舟错误正文，用于结构化错误日志（输出到控制台/文件/Graylog）。

对照火山方舟公共错误码（模型调用 API）：
https://www.volcengine.com/docs/82379/1299023

Coding Plan 额外会出现文档短表里没有的 ``AccountQuotaExceeded`` /
``RequestBurstTooFast``。额度耗尽的 message 里带恢复时间，没有已用/总量数字。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 「You have exceeded the 5-hour usage quota. It will reset at 2026-03-11 23:59:59 +0800 CST.」
_QUOTA_MESSAGE_RE = re.compile(
    r"exceeded the (?P<window>5-hour|weekly|monthly) usage quota\.\s*"
    r"It will reset at (?P<reset>.+?)(?:\.|\s+We recommend|\s+Request id|$)",
    re.IGNORECASE,
)

_WINDOW_LABEL = {
    "5-hour": "5小时",
    "weekly": "每周",
    "monthly": "每月",
}

# 文档「含义」列。收录常见的限流、额度、欠费、过载等。
_ERROR_MEANING: dict[str, str] = {
    "RateLimitExceeded.EndpointRPMExceeded": "推理接入点已超过 RPM 限制，请稍后重试。",
    "RateLimitExceeded.EndpointTPMExceeded": "推理接入点已超过 TPM 限制，请稍后重试。",
    "RateLimitExceeded.FoundationModelRPMExceeded": "账号下该基础模型已超过 RPM 限制，请稍后重试。",
    "RateLimitExceeded.FoundationModelTPMExceeded": "账号下该基础模型已超过 TPM 限制，请稍后重试。",
    "ModelAccountRpmRateLimitExceeded": "已超过账户模型 RPM 限制，请稍后重试。",
    "ModelAccountTpmRateLimitExceeded": "已超过账户模型 TPM 限制，请稍后重试。",
    "QuotaExceeded": "免费试用额度已用完，请到方舟控制台开通对应模型。",
    "ServerOverloaded": "服务资源紧张，请稍后重试。",
    "AccountOverdueError": "账号欠费，请到火山交易中心充值。",
    "AccountQuotaExceeded": "Coding Plan 套餐额度已耗尽，等待该周期重置或升级套餐。",
    "RequestBurstTooFast": "请求突发触发保护，请放慢调用后再试。",
}


def _meaning_for(code: str) -> str:
    if not code:
        return ""
    if code in _ERROR_MEANING:
        return _ERROR_MEANING[code]
    if code.startswith("RateLimitExceeded"):
        return "触发方舟限流，请稍后重试。"
    return ""


def parse_provider_error(body: str) -> dict[str, Any]:
    """解析 429/4xx 正文。能对上错误码表时附上中文含义，便于日志采集（Graylog）。"""
    raw = (body or "").strip()
    if not raw:
        return {}

    code = ""
    message = raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip()
            message = str(err.get("message") or raw).strip()
        elif isinstance(err, str) and err.strip():
            message = err.strip()

    fields: dict[str, Any] = {"provider_message": message[:2000]}
    if code:
        fields["provider_error_code"] = code

    meaning = _meaning_for(code)
    if meaning:
        fields["error_meaning"] = meaning

    matched = _QUOTA_MESSAGE_RE.search(message)
    exhausted = code == "AccountQuotaExceeded" or matched is not None
    if exhausted:
        window = matched.group("window").lower() if matched else ""
        reset_at = matched.group("reset").strip() if matched else ""
        fields["quota_exhausted"] = True
        if window:
            fields["quota_window"] = window
        if reset_at:
            fields["quota_reset_at"] = reset_at
        label = _WINDOW_LABEL.get(window, window or "套餐")
        when = reset_at or "未知"
        fields["quota_summary"] = f"{label}额度已耗尽，恢复时间 {when}"

    return fields
