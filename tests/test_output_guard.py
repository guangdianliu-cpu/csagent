"""guardrail.output_guard.OutputGuard：对内场景骨架放行。"""

from __future__ import annotations

from csagent.guardrail.output_guard import OutputGuard

guard = OutputGuard()


def test_normal_reply_passes_unchanged():
    text = "您好，您当前持有XX基金，净值为1.234元。"
    verdict = guard.review(text)
    assert verdict.passed is True
    assert verdict.sanitized_text == text


def test_promise_style_text_is_not_blocked():
    text = "这款产品保本保收益，请放心购买。"
    verdict = guard.review(text)
    assert verdict.passed is True
    assert verdict.sanitized_text == text


def test_empty_reply_passes():
    verdict = guard.review("")
    assert verdict.passed is True


def test_internal_field_style_text_is_not_blocked():
    text = "本次查询 trace_id=abc123，memory_snapshot={}"
    verdict = guard.review(text)
    assert verdict.passed is True
    assert verdict.sanitized_text == text
