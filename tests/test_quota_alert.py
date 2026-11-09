"""方舟错误码与限流/额度解析测试（供日志采集/Graylog 使用）。"""

from __future__ import annotations

import json

from csagent.infra.quota_alert import parse_provider_error


def test_parse_five_hour_quota_reset() -> None:
    body = json.dumps(
        {
            "error": {
                "code": "AccountQuotaExceeded",
                "message": (
                    "You have exceeded the 5-hour usage quota. "
                    "It will reset at 2026-09-23 14:00:00 +0800 CST. "
                    "We recommend upgrading your plan for more quota."
                ),
                "type": "TooManyRequests",
            }
        }
    )
    fields = parse_provider_error(body)
    assert fields["quota_exhausted"] is True
    assert fields["provider_error_code"] == "AccountQuotaExceeded"
    assert fields["quota_window"] == "5-hour"
    assert fields["quota_reset_at"] == "2026-09-23 14:00:00 +0800 CST"
    assert "5小时额度已耗尽" in fields["quota_summary"]
    assert "套餐额度已耗尽" in fields["error_meaning"]


def test_rpm_limit_uses_doc_meaning() -> None:
    message = (
        "The Requests Per Minute (RPM) limit of the associated endpoint "
        "for your account has been exceeded. Request ID: abc"
    )
    body = json.dumps(
        {
            "error": {
                "code": "RateLimitExceeded.EndpointRPMExceeded",
                "message": message,
                "type": "TooManyRequests",
            }
        }
    )
    fields = parse_provider_error(body)
    assert "RPM" in fields["error_meaning"]
    assert fields["provider_message"] == message
    assert "quota_exhausted" not in fields


def test_invalid_parameter_has_no_meaning_or_quota() -> None:
    body = json.dumps(
        {
            "error": {
                "code": "InvalidParameter",
                "message": "One or more parameters specified in the request are not valid.",
                "type": "BadRequest",
            }
        }
    )
    fields = parse_provider_error(body)
    assert fields["provider_error_code"] == "InvalidParameter"
    assert "quota_exhausted" not in fields
    assert "error_meaning" not in fields
