"""企微确认卡片：归属校验、批准 / 拒绝后续跑、一次只发能点的那张、卡片内容。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.tools.approvals import ApprovalStatus, ApprovalStore, ClaimReason, approval_chain_var
from csagent.wecom.cards import (
    CANCEL_ACTION,
    CONFIRM_ACTION,
    build_confirmation_card,
    parse_template_card_event,
)

from tests.approval_helpers import (
    ResumeGateway,
    card_frame,
    challenge_registry,
    click_card,
    group_scope,
    raise_card,
    sent_bodies,
    wecom_bridge,
)

# ---------------------------------------------------------------- 归属校验


@pytest.mark.asyncio
async def test_wecom_card_cannot_approve_action_raised_on_the_web() -> None:
    registry = challenge_registry()
    # 网页端 uid 恰好与企微 userid 同名
    pending = await raise_card(registry, group_scope(channel="http_api", session_id="web:owner"))
    agent = await click_card(registry, card_frame(pending.task_id, operator="owner", chatid="g1"))
    agent.execute_approved_action.assert_not_awaited()
    assert registry.approvals.lookup(pending.task_id).status == ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_wecom_card_cannot_be_approved_from_another_group() -> None:
    registry = challenge_registry()
    pending = await raise_card(registry, group_scope())
    other = await click_card(registry, card_frame(pending.task_id, operator="owner", chatid="g2"))
    other.execute_approved_action.assert_not_awaited()
    same = await click_card(registry, card_frame(pending.task_id, operator="owner", chatid="g1"))
    same.execute_approved_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_wecom_group_card_can_be_handled_by_any_member() -> None:
    registry = challenge_registry()
    pending = await raise_card(registry, group_scope())
    agent = await click_card(registry, card_frame(pending.task_id, operator="teammate", chatid="g1"))
    agent.execute_approved_action.assert_awaited_once()
    assert registry.approvals.lookup(pending.task_id).operator == "teammate"
    # 没有「无权审批」的提示
    texts = [str(call) for call in agent.client.send_message.await_args_list]
    assert not any("无权" in text for text in texts)


@pytest.mark.asyncio
async def test_wecom_single_chat_card_is_only_for_the_initiator() -> None:
    """单聊卡片只有本人收得到；别人点（被转发了）不认。"""
    registry = challenge_registry()
    store = registry.approvals
    pending = store.request("wecom:owner", "send_file", {"path": "a"}, summary="a", rule_id="r",
                            user_id="owner", channel="wecom", chat_type="single")
    other = await click_card(registry, card_frame(pending.task_id, operator="intruder",
                                                  chatid="intruder", chattype="single"))
    other.execute_approved_action.assert_not_awaited()
    assert store.lookup(pending.task_id).status == ApprovalStatus.PENDING
    owner = await click_card(registry, card_frame(pending.task_id, operator="owner",
                                                  chatid="owner", chattype="single"))
    owner.execute_approved_action.assert_awaited_once()


# ---------------------------------------------------------------- 批准 / 拒绝后续跑


@pytest.mark.asyncio
async def test_wecom_approval_runs_continuation_and_pushes_answer_and_next_card() -> None:
    registry = challenge_registry()
    pending = await raise_card(registry, group_scope())
    # 同一任务里排在后面的一张：前面这张处理完之前不发
    nxt = registry.approvals.request(pending.session_id, "http_call", {"url": "x"},
                                     summary="外呼", rule_id="r", user_id="owner",
                                     channel="wecom", chat_type="group", chat_id="g1",
                                     chain=pending.chain)
    gateway = ResumeGateway()
    agent = await click_card(registry, card_frame(pending.task_id, operator="owner", chatid="g1"),
                             gateway)

    sent = gateway.messages[0]
    assert sent.session_id == pending.session_id
    assert sent.text.startswith("[系统] 已执行")
    assert sent.metadata["transcript_text"].startswith("【已批准】")
    assert sent.principal["user_id"] == "owner" and sent.principal["chat_id"] == "g1"
    bodies = sent_bodies(agent.client)
    assert bodies[0]["msgtype"] == "markdown"
    assert bodies[0]["markdown"]["content"] == "报表已发到群里，接下来还要外呼。"
    # 回答之后再发下一张（续跑里吐的、确认簿里没有的 task_next 不会发）
    assert bodies[1]["msgtype"] == "template_card"
    assert bodies[1]["template_card"]["task_id"] == nxt.task_id
    assert len(bodies) == 2
    # 续跑说明已取走，不会再续一次
    assert registry.approvals.take_continuation(pending.session_id, pending.task_id) is None


@pytest.mark.asyncio
async def test_wecom_rejection_runs_continuation_without_executing() -> None:
    registry = challenge_registry()
    pending = await raise_card(registry, group_scope())
    gateway = ResumeGateway()
    agent = await click_card(
        registry, card_frame(pending.task_id, operator="owner", chatid="g1", approve=False), gateway
    )

    agent.execute_approved_action.assert_not_awaited()
    agent.client.update_template_card.assert_awaited_once()
    sent = gateway.messages[0]
    assert sent.text.startswith("[系统] 用户拒绝了此前暂停的操作")
    assert sent.metadata["transcript_text"].startswith("【已拒绝】")
    assert sent_bodies(agent.client)[0]["markdown"]["content"] == "报表已发到群里，接下来还要外呼。"


@pytest.mark.asyncio
async def test_card_raised_in_continuation_joins_the_original_chain() -> None:
    registry = challenge_registry()
    first = registry.approvals.request(
        "wecom:group:g1:owner", "send_file", {"path": "a.xlsx"}, summary="a", rule_id="group-send",
        user_id="owner", channel="wecom", chat_type="group", chat_id="g1", chain="turn-1",
    )
    chains: list[str] = []

    class _ChainGateway(ResumeGateway):
        async def handle(self, message):
            chains.append(approval_chain_var.get())
            yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="好了")

    await click_card(registry, card_frame(first.task_id, operator="owner", chatid="g1"),
                     _ChainGateway())
    assert chains == ["turn-1"]


# ---------------------------------------------------------------- 先后顺序


@pytest.mark.asyncio
async def test_wecom_later_card_is_blocked_until_earlier_one_is_settled() -> None:
    registry = challenge_registry()
    await raise_card(registry, group_scope(), "a.xlsx")
    second = await raise_card(registry, group_scope(), "b.xlsx")

    agent = await click_card(registry, card_frame(second.task_id, operator="owner", chatid="g1"))
    agent.execute_approved_action.assert_not_awaited()
    agent.client.update_template_card.assert_not_awaited()  # 卡片保持可点
    notice = agent.client.send_message.await_args.kwargs["body"]["markdown"]["content"]
    assert "请先处理上面那张确认卡片" in notice and "a.xlsx" in notice
    assert registry.approvals.lookup(second.task_id).status == ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_wecom_next_card_stays_blocked_until_continuation_finishes() -> None:
    """企微同一会话多轮并行：前一张的续跑没结束，后一张批不了；结束后（含失败）放开。"""
    registry = challenge_registry()
    store = registry.approvals
    first = await raise_card(registry, group_scope())
    second = store.request(first.session_id, "send_file", {"path": "b.xlsx"},
                           summary="b", rule_id="group-send", user_id="owner")
    seen: dict = {}

    class _ProbeGateway(ResumeGateway):
        async def handle(self, message):
            seen["during"] = store.claim(second.task_id, "owner").reason
            raise RuntimeError("续跑失败")
            yield  # pragma: no cover

    await click_card(registry, card_frame(first.task_id, operator="owner", chatid="g1"),
                     _ProbeGateway())
    assert seen["during"] == ClaimReason.BLOCKED
    assert store.claim(second.task_id, "owner").reason == ClaimReason.OK


@pytest.mark.asyncio
async def test_wecom_sends_only_the_first_card_of_a_task_and_notes_the_rest() -> None:
    """企微卡片发出后改不了：同一任务里一次只发能点的那张，并注明后面还有几项。"""
    store = ApprovalStore()
    kw = dict(rule_id="r", user_id="owner", channel="wecom", chat_type="group", chat_id="g1")
    a = store.request("s1", "send_file", {"path": "a"}, summary="a", chain="t1", **kw)
    b = store.request("s1", "send_file", {"path": "b"}, summary="b", chain="t1", **kw)
    other = store.request("s1", "send_file", {"path": "c"}, summary="c", chain="t2", **kw)
    bridge, _agent = wecom_bridge(store)

    await bridge._send_ready_cards("s1", frame={"headers": {}})

    cards = [call.args[1] for call in bridge._client.reply_template_card.await_args_list]
    assert [card["task_id"] for card in cards] == [a.task_id, other.task_id]  # 另一个任务不受影响
    rows = cards[0]["horizontal_content_list"]
    assert {"keyname": "后续确认", "value": "还有 1 项，处理完这张后发出"} in rows
    # 有效期按确认簿里的过期时刻显示
    assert any(row["keyname"] == "有效时间" and row["value"].endswith("前有效") for row in rows)
    # 再调一次不会重发；前面那张处理完，后面那张才发
    await bridge._send_ready_cards("s1", frame={"headers": {}})
    assert bridge._client.reply_template_card.await_count == 2
    store.cancel(a.task_id, "owner")
    await bridge._send_ready_cards("s1", frame={"headers": {}})
    assert bridge._client.reply_template_card.await_args.args[1]["task_id"] == b.task_id


# ---------------------------------------------------------------- 卡片内容


def test_confirmation_card_uses_task_id_not_the_button_key():
    # 北京时间 09:49 = UTC 01:49；不随运行测试的机器时区变化
    expires_at = datetime(2026, 10, 13, 1, 49, tzinfo=timezone.utc).timestamp()
    card = build_confirmation_card(
        task_id="task_abc",
        summary="把报表发到群里",
        tool_name="send_file",
        reason="群里可见",
        from_user="owner",
        chat_type="group",
        expires_at=expires_at,
    )
    assert card["task_id"] == "task_abc"
    assert {b["key"] for b in card["button_list"]} == {CONFIRM_ACTION, CANCEL_ACTION}
    assert {"keyname": "有效时间", "value": "09:49 前有效"} in card["horizontal_content_list"]
    assert card["main_title"]["title"] == "需要您确认这项操作"
    click = parse_template_card_event(
        {
            "body": {
                "chattype": "group",
                "chatid": "wr123",
                "from": {"userid": "owner"},
                "event": {
                    "template_card_event": {
                        "task_id": "task_abc",
                        "event_key": CONFIRM_ACTION,
                    }
                },
            }
        }
    )
    assert click["task_id"] == "task_abc"
    assert click["event_key"] == CONFIRM_ACTION
    assert click["operator"] == "owner"
    assert click["chatid"] == "wr123"


def test_card_times_are_beijing_time_regardless_of_server_timezone(monkeypatch):
    """服务器是 UTC 时卡片也显示北京时间。"""
    import time as time_mod

    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time_mod, "tzset"):
        time_mod.tzset()
    try:
        card = build_confirmation_card(
            task_id="t", summary="s", tool_name="send_file", reason="r",
            from_user="u", chat_type="single",
            expires_at=datetime(2026, 10, 13, 15, 30, tzinfo=timezone.utc).timestamp(),
        )
    finally:
        monkeypatch.undo()
        if hasattr(time_mod, "tzset"):
            time_mod.tzset()
    assert {"keyname": "有效时间", "value": "23:30 前有效"} in card["horizontal_content_list"]
