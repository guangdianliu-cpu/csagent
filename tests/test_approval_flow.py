"""网页与企微共用的卡片点击处理（channels/approval_flow.py）。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.audit.security_logger import security_audit
from csagent.channels.approval_flow import (
    EXPIRED_NOTICE,
    FORBIDDEN_NOTICE,
    REJECTED_NOTICE,
    ClickOutcome,
    click_notice,
    resolve_click,
    resume_transcript,
    run_approved,
)
from csagent.tools.approvals import ApprovalStatus, ApprovalStore


def _store_with_card(**kwargs):
    store = ApprovalStore()
    pending = store.request(
        "s1", "send_file", {"path": "a.xlsx"}, summary="把报表发到群里", rule_id="group-send",
        user_id="owner", **kwargs,
    )
    return store, pending


def _security_events() -> list[dict]:
    events: list[dict] = []
    for path in sorted(security_audit().audit_dir.glob("security-*.jsonl")):
        events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return events


def test_card_from_elsewhere_is_reported_as_missing_and_left_untouched():
    store, pending = _store_with_card()
    click = resolve_click(
        store, pending.task_id, approve=True, operator="owner", belongs_here=lambda _item: False
    )
    assert click.outcome == ClickOutcome.MISSING
    assert click_notice(click) == EXPIRED_NOTICE
    assert store.lookup(pending.task_id).status == ApprovalStatus.PENDING


def test_rejection_is_recorded_in_the_security_audit():
    store, pending = _store_with_card()
    click = resolve_click(
        store, pending.task_id, approve=False, operator="teammate", belongs_here=lambda _item: True
    )
    assert click.outcome == ClickOutcome.REJECTED
    assert click_notice(click) == REJECTED_NOTICE
    event = next(e for e in _security_events() if e["event"] == "tool_challenge_rejected")
    assert event["actor"] == "teammate"
    assert event["tool"] == "send_file" and event["summary"] == "把报表发到群里"


def test_approval_claims_the_card_without_recording_a_rejection():
    store, pending = _store_with_card()
    click = resolve_click(
        store, pending.task_id, approve=True, operator="owner", belongs_here=lambda _item: True
    )
    assert click.outcome == ClickOutcome.APPROVED and click.pending.task_id == pending.task_id
    assert click_notice(click) == ""
    assert store.lookup(pending.task_id).status == ApprovalStatus.RUNNING
    assert not any(e["event"] == "tool_challenge_rejected" for e in _security_events())


def test_click_notices_name_the_blocker_and_who_handled_it():
    store = ApprovalStore()
    first = store.request("s1", "send_file", {"path": "a"}, summary="先生成报表", rule_id="r",
                          user_id="u")
    second = store.request("s1", "send_file", {"path": "b"}, summary="再发到群里", rule_id="r",
                           user_id="u")
    accept = lambda _item: True  # noqa: E731

    blocked = resolve_click(store, second.task_id, approve=True, operator="u", belongs_here=accept)
    assert blocked.outcome == ClickOutcome.BLOCKED and blocked.pending.task_id == first.task_id
    assert "先生成报表" in click_notice(blocked)

    resolve_click(store, first.task_id, approve=False, operator="alice", belongs_here=accept)
    again = resolve_click(store, first.task_id, approve=True, operator="bob", belongs_here=accept)
    assert again.outcome == ClickOutcome.DUPLICATE
    assert click_notice(again) == "该操作已由 alice 处理，不能再次变更。"
    assert click_notice(again, mention=lambda uid: f"<@{uid}>") == "该操作已由 <@alice> 处理，不能再次变更。"

    nobody = resolve_click(store, second.task_id, approve=True, operator="", belongs_here=accept)
    assert nobody.outcome == ClickOutcome.FORBIDDEN
    assert click_notice(nobody) == FORBIDDEN_NOTICE


@pytest.mark.asyncio
async def test_failed_execution_releases_the_card_and_explains():
    store, pending = _store_with_card()
    store.claim(pending.task_id, "owner")
    agent = MagicMock()
    agent.execute_approved_action = AsyncMock(side_effect=RuntimeError("bridge down"))

    run = await run_approved(agent, store, pending)

    assert run.action is None
    assert run.error == "操作未能完成：bridge down"
    assert store.lookup(pending.task_id) is None  # 不会停在「执行中」


def test_resume_transcript_shows_the_decision_not_the_model_note():
    store, pending = _store_with_card()
    store.cancel(pending.task_id, "owner")
    rejected = store.take_continuation("s1", pending.task_id)
    assert resume_transcript(rejected) == "【已拒绝】把报表发到群里"

    store2, pending2 = _store_with_card()
    store2.claim(pending2.task_id, "owner")
    store2.complete(pending2.task_id, result_text="ok", result_status="success", continuation="n")
    approved = store2.take_continuation("s1", pending2.task_id)
    assert resume_transcript(approved) == "【已批准】把报表发到群里"
