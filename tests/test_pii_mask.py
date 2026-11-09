"""PII 脱敏（M2.2）：使用 configs/guardrail.yaml 真实规则验证手机号/身份证/银行卡脱敏。"""

from __future__ import annotations

from csagent.guardrail.config_loader import load_guardrail_config
from csagent.guardrail.output_guard import OutputGuard


def _guard() -> OutputGuard:
    cfg = load_guardrail_config()
    return OutputGuard.from_config(cfg.get("output_rules"))


def test_config_loader_reads_guardrail_yaml():
    cfg = load_guardrail_config()
    assert cfg["enabled"] is True
    assert any(r.get("name") == "pii_phone" for r in cfg["output_rules"])


def test_phone_number_is_masked():
    guard = _guard()
    verdict = guard.review("请联系我，手机号是13812345678")
    assert verdict.passed is True
    assert "13812345678" not in (verdict.sanitized_text or "")


def test_id_card_is_masked():
    guard = _guard()
    verdict = guard.review("身份证号：11010519491231002X，请核对")
    assert verdict.passed is True
    assert "11010519491231002X" not in (verdict.sanitized_text or "")


def test_bank_card_is_masked():
    guard = _guard()
    verdict = guard.review("银行卡号6222021234567890123，用于转账")
    assert verdict.passed is True
    assert "6222021234567890123" not in (verdict.sanitized_text or "")


def test_sensitive_keyword_warns_but_does_not_alter_text():
    guard = _guard()
    text = "您的密码需要定期更新"
    verdict = guard.review(text)
    assert verdict.passed is True
    assert verdict.sanitized_text == text


def test_normal_text_is_unaffected():
    guard = _guard()
    text = "您好，您当前持有XX基金，净值为1.234元。"
    verdict = guard.review(text)
    assert verdict.passed is True
    assert verdict.sanitized_text == text
