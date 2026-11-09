"""确认卡片点击的统一处理：网页与企微共用同一套认领 / 作废 / 执行规则。

一次点击的处理：

1. :func:`resolve_click`：找到卡片、确认它归这个渠道管，再认领（批准）或作废（拒绝）。
   找不到和不归这里管的一律按「已过期或不存在」答复，不区分「没有」和「不是你的」；
   拒绝在这里记安全审计（批准的执行结果由 Agent 记）。
2. :func:`run_approved`：批准的卡片按登记时保存的参数执行一次。Agent 负责审计、脱敏、
   记下结果与续跑说明；执行抛异常时作废卡片，不让它停在「执行中」。
3. 续跑与放开后面的卡片由渠道各自安排，两边的差别是有意的：

   - 网页：执行完立即放开同一任务里后面的卡片（「同一会话同时只跑一轮」已经保证了顺序），
     前端随后带 ``resume_task_id`` 再发一轮续跑；
   - 企微：卡片回调里直接续跑，续跑结束才放开后面的卡片并主动推送下一张
     （企微同一会话的多轮可以并行，只能靠确认簿挡住）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import structlog

from csagent.agent import Agent, ApprovedAction
from csagent.audit.security_logger import record_security_event
from csagent.tools.approvals import (
    ApprovalStatus,
    ApprovalStore,
    ClaimReason,
    PendingApproval,
    blocked_notice,
)

logger = structlog.get_logger(__name__)

EXPIRED_NOTICE = "该确认已过期或不存在，操作没有执行。"
REJECTED_NOTICE = "操作已取消，不会执行。"
FORBIDDEN_NOTICE = "无法确认您的身份，请重新登录后再点。"


class ClickOutcome(StrEnum):
    """一次点击的结果。"""

    APPROVED = "approved"  # 已认领，等 run_approved 执行
    REJECTED = "rejected"  # 已作废，续跑说明已备好
    BLOCKED = "blocked"  # 同一任务里前面还有没处理完的卡片
    DUPLICATE = "duplicate"  # 已经有人处理过
    MISSING = "missing"  # 不存在、已过期，或不归这个渠道管
    FORBIDDEN = "forbidden"  # 拿不到点击人


_OUTCOME_BY_REASON = {
    ClaimReason.BLOCKED: ClickOutcome.BLOCKED,
    ClaimReason.DUPLICATE: ClickOutcome.DUPLICATE,
    ClaimReason.FORBIDDEN: ClickOutcome.FORBIDDEN,
    ClaimReason.MISSING: ClickOutcome.MISSING,
}


@dataclass
class CardClick:
    """点击处理结果。

    ``pending``：APPROVED / REJECTED / DUPLICATE 时是被点的那张；BLOCKED 时是挡在前面的那张；
    MISSING / FORBIDDEN 时为 None 或无意义。
    """

    outcome: ClickOutcome
    pending: PendingApproval | None = None


def resolve_click(
    store: ApprovalStore,
    task_id: str,
    *,
    approve: bool,
    operator: str,
    belongs_here: Callable[[PendingApproval], bool],
) -> CardClick:
    """认领或作废一张卡片。

    :param belongs_here: 渠道的归属校验（网页：同一会话；企微：企微发起、同一个群 /
        单聊本人）。不通过按 MISSING 处理。
    """
    target = store.lookup(task_id)
    if target is None or not belongs_here(target):
        return CardClick(ClickOutcome.MISSING)
    claim = store.claim(task_id, operator) if approve else store.cancel(task_id, operator)
    if claim.reason != ClaimReason.OK:
        outcome = _OUTCOME_BY_REASON[claim.reason]
        if outcome == ClickOutcome.BLOCKED and claim.pending is None:
            outcome = ClickOutcome.MISSING
        return CardClick(outcome, claim.pending)
    pending = claim.pending
    assert pending is not None  # ClaimReason.OK 一定带着这张卡片
    if approve:
        return CardClick(ClickOutcome.APPROVED, pending)
    record_security_event(
        "tool_challenge_rejected",
        outcome="success",
        actor=operator,
        session_id=pending.session_id,
        tool=pending.tool_name,
        rule_id=pending.rule_id,
        summary=pending.summary,
    )
    return CardClick(ClickOutcome.REJECTED, pending)


def click_notice(click: CardClick, *, mention: Callable[[str], str] = str) -> str:
    """给点卡片的人看的一句话；APPROVED 不需要提示，返回空串。

    :param mention: 把用户 id 渲染成渠道里的称呼（企微用纯文字 ``@userid``）
    """
    if click.outcome == ClickOutcome.APPROVED:
        return ""
    if click.outcome == ClickOutcome.REJECTED:
        return REJECTED_NOTICE
    if click.outcome == ClickOutcome.FORBIDDEN:
        return FORBIDDEN_NOTICE
    if click.outcome == ClickOutcome.BLOCKED and click.pending is not None:
        return blocked_notice(click.pending)
    if click.outcome == ClickOutcome.DUPLICATE:
        operator = click.pending.operator if click.pending is not None else ""
        who = mention(operator) if operator else "其他人"
        return f"该操作已由 {who} 处理，不能再次变更。"
    return EXPIRED_NOTICE


def resume_transcript(pending: PendingApproval) -> str:
    """续跑那一轮在对话记录里显示的用户侧文字（正文是给模型的说明，不给人看）。"""
    verdict = "已拒绝" if pending.status == ApprovalStatus.CANCELLED else "已批准"
    return f"【{verdict}】{pending.summary}"


@dataclass
class ApprovalRun:
    """批准后执行一次的结果：``action`` 为 None 时 ``error`` 是给用户看的说明。"""

    action: ApprovedAction | None
    error: str = ""


async def run_approved(agent: Agent, store: ApprovalStore, pending: PendingApproval) -> ApprovalRun:
    """执行已认领的卡片。抛异常时作废卡片（Agent 自己也会放开，这里兜底）并返回说明。"""
    try:
        return ApprovalRun(await agent.execute_approved_action(pending))
    except Exception as exc:  # noqa: BLE001 - 执行失败也要把卡片从「执行中」放开
        logger.exception("approved_action_failed", task_id=pending.task_id, error=str(exc))
        store.finish(pending.task_id)
        return ApprovalRun(None, f"操作未能完成：{exc}")
