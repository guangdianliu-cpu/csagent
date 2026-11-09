"""需要用户确认的操作（策略 ``challenge``）的卡片登记。

流程：

1. 策略判定为 challenge → ``request()`` 登记一条待确认操作，绑定会话、工具和原始参数，
   有效期 ``DEFAULT_TTL_SECONDS``（卡片上显示具体的过期时刻），并发一张卡片
   （企微互动卡片 / 网页批准按钮）。同一操作再被调用复用这条登记（同一个 task_id），
   哪张卡片上点都一样；
2. 点批准 → ``claim()`` 校验先后顺序并占住这条登记，调用方按保存的参数执行，
   执行完 ``complete()`` 记下结果；
3. 结果有两个用处：
   - **回灌**：``take_continuation()`` 交出一次性的续跑说明，渠道拿它起一轮新的对话，
     让模型带着执行结果把原任务做完；
   - **防重放**：续跑时模型若按原参数再调一次，``executed()`` 命中，直接回放结果，
     不再执行、也不再要确认；参数变了就是另一张卡片；
4. 点拒绝 → ``cancel()`` 作废这条登记，工具不会跑；同样留一份一次性的续跑说明，
   让模型知道被拒了（不再发起、把不依赖它的部分做完），否则它的记忆停在「等待确认」。

**先后顺序**：同一**任务链**里的卡片按登记先后批准。任务链 = 发起它的那一轮
（trace_id），批准后续跑那一轮出的卡片沿用原任务的链。同一会话里并行的几个任务
是互不依赖的，各走各的，不互相挡。链内：前面的还没处理（没批也没拒）、
还在执行、或执行完但续跑那一轮还没结束（``settle()`` 之前），后面的批不了——后面的
操作往往依赖前面的结果（先生成、再发送）。续跑若丢了（没人调 ``settle``），最多挡
``settle_timeout_seconds``。拒绝不受顺序限制。

全部状态在进程内（重启即清空，等同全部过期）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

DEFAULT_TTL_SECONDS = 600

# 续跑那一轮由渠道设成原卡片的任务链，这一轮再出的卡片就排在原任务后面；
# 普通一轮为空，注册中心用本轮 trace_id 起一条新链
approval_chain_var: ContextVar[str] = ContextVar("approval_chain", default="")


class ApprovalStatus(StrEnum):
    """一张卡片的状态。"""

    PENDING = "pending"  # 等用户点
    RUNNING = "running"  # 已批准，正在执行
    DONE = "done"  # 执行结束（成功或失败）：等续跑取走结果，结果留作防重放
    CANCELLED = "cancelled"  # 已拒绝：等续跑取走「被拒」说明


class ClaimReason(StrEnum):
    """一次点击（批准或拒绝）的处理结果。"""

    OK = "ok"
    MISSING = "missing"  # 不存在或已过期
    FORBIDDEN = "forbidden"  # 拿不到点击人，无从记录是谁处理的
    DUPLICATE = "duplicate"  # 已经处理过
    BLOCKED = "blocked"  # 同一任务里前面还有没处理完的卡片


def arguments_digest(tool_name: str, arguments: dict[str, Any]) -> str:
    """工具名 + 参数的稳定摘要（键排序），用来判断是不是同一张卡片。"""
    canonical = json.dumps(
        {"tool": tool_name, "args": arguments}, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class PendingApproval:
    """一条待确认的操作。卡片和点击靠 ``task_id`` 对上。"""

    task_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    digest: str
    summary: str
    rule_id: str
    user_id: str
    auth_method: str
    channel: str
    chat_type: str
    chat_id: str
    agent_name: str
    created_at: float
    expires_at: float
    # 登记序号：判先后顺序用它而不是 created_at——Windows 时钟精度约 15ms，同一批并行的
    # 工具调用会拿到相同时间戳
    seq: int = 0
    # 任务链：只有同一条链里的卡片才排先后
    chain: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    operator: str = ""
    # 策略给出的原因，刷新页面后卡片还要能显示
    reason: str = ""
    # 执行结果（防重放时回放给模型）与一次性的续跑说明（回灌给模型继续原任务）
    result_text: str = ""
    result_status: str = ""
    continuation: str = ""
    # 续跑那一轮结束了没有；没结束前挡住后面的卡片，最晚挡到 settle_by
    settled: bool = False
    settle_by: float = 0.0
    # 企微：卡片已经发给用户。企微发出的卡片事后改不了，排在后面的卡片要等前面的
    # 处理完才发（见 take_ready_cards），用户始终只看到一张能点的
    card_sent: bool = False

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) > self.expires_at


def rejection_note(summary: str) -> str:
    """卡片被拒绝后，续跑那一轮发给模型的说明。"""
    return (
        f"[系统] 用户拒绝了此前暂停的操作：{summary}。这项操作没有执行。本轮不要再发起它，"
        "也不要换个参数绕过去。请简短告诉用户已取消；原任务里不依赖它的部分照常完成，"
        "依赖它的部分说明做不了。原任务里没有别的事要做时，确认一句已取消即可。"
        "这次拒绝只针对这一次：以后用户明确提出新的请求时，照常发起（会重新出确认卡片）。"
    )


def waiting_note(items: list[PendingApproval]) -> str:
    """续跑说明的附注：同一任务里已经发出、还在等用户确认的操作。"""
    lines = "\n".join(f"- {item.summary}" for item in items)
    return (
        "\n\n[系统] 以下操作已经登记并发出了确认卡片，正在等用户确认，"
        "用户确认后系统会执行并把结果交给你：\n"
        f"{lines}\n"
        "不要再调用它们（换个参数写法也不行），也不要说它们已经完成；"
        "在回复里提一句仍在等用户确认即可。"
    )


def blocked_notice(blocker: PendingApproval) -> str:
    """后面的卡片被前面那张挡住时给用户的提示。说人话，不贴工具名和参数。"""
    if blocker.status in (ApprovalStatus.RUNNING, ApprovalStatus.DONE):
        return "上一项操作还在处理中，请稍等片刻再点这张卡片。"
    summary = (blocker.summary or "").strip()
    if len(summary) > 30:
        summary = summary[:29] + "…"
    hint = f"（{summary}）" if summary else ""
    return f"请先处理上面那张确认卡片{hint}，再点这张。"


@dataclass
class CardClaim:
    """卡片点击的处理结果（``reason`` 见 :class:`ClaimReason`）。

    谁能点由渠道把关（企微：群卡片限同一个群、单聊卡片限本人；网页：限本会话），
    确认簿只记下点的人，不再要求必须是发起人——群里的卡片群成员都可以处理。
    ``pending`` 在 ok 时是可以执行或作废的那一条；blocked 时是挡在前面的那一条。
    """

    reason: ClaimReason
    pending: PendingApproval | None = None


class ApprovalStore:
    """进程内的确认卡片登记簿（线程安全）。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        settle_timeout_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._ttl = ttl_seconds
        self._settle_timeout = settle_timeout_seconds
        self._lock = threading.Lock()
        self._items: dict[str, list[PendingApproval]] = {}
        self._by_task: dict[str, PendingApproval] = {}
        self._seq = itertools.count(1)

    def _live(self, session_id: str, now: float) -> list[PendingApproval]:
        fresh: list[PendingApproval] = []
        for item in self._items.get(session_id, []):
            if item.expired(now):
                self._by_task.pop(item.task_id, None)
            else:
                fresh.append(item)
        self._items[session_id] = fresh
        return fresh

    def _drop(self, item: PendingApproval) -> None:
        self._by_task.pop(item.task_id, None)
        items = self._items.get(item.session_id, [])
        self._items[item.session_id] = [p for p in items if p.task_id != item.task_id]

    def request(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        summary: str,
        rule_id: str,
        user_id: str = "",
        auth_method: str = "",
        channel: str = "",
        chat_type: str = "",
        chat_id: str = "",
        agent_name: str = "agent",
        reason: str = "",
        chain: str = "",
        identity: dict[str, Any] | None = None,
    ) -> PendingApproval:
        """登记一条待确认操作。同一会话里参数没变、且还没点过的卡片会复用。

        ``identity``：判断是不是同一项操作用的参数（工具规整过的写法，见
        ``BaseTool.approval_identity``），缺省用 ``arguments``；执行仍按 ``arguments``。
        """
        now = time.time()
        digest = arguments_digest(tool_name, identity if identity is not None else arguments)
        with self._lock:
            items = self._live(session_id, now)
            for item in items:
                if item.digest == digest and item.status == ApprovalStatus.PENDING:
                    return item
            pending = PendingApproval(
                task_id=f"task_{secrets.token_hex(8)}",
                session_id=session_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                digest=digest,
                summary=summary,
                rule_id=rule_id,
                user_id=user_id,
                auth_method=auth_method,
                channel=channel,
                chat_type=chat_type,
                chat_id=chat_id,
                agent_name=agent_name or "agent",
                reason=reason,
                chain=chain,
                created_at=now,
                expires_at=now + self._ttl,
                seq=next(self._seq),
            )
            items.append(pending)
            self._by_task[pending.task_id] = pending
            return pending

    def pending_for(
        self, session_id: str, tool_name: str, identity: dict[str, Any]
    ) -> PendingApproval | None:
        """本会话里同一项操作（按 ``identity`` 判断）已登记、还没点过的那张卡片。"""
        now = time.time()
        digest = arguments_digest(tool_name, identity)
        with self._lock:
            for item in self._live(session_id, now):
                if item.digest == digest and item.status == ApprovalStatus.PENDING:
                    return item
        return None

    def _lookup(self, task_id: str, now: float) -> PendingApproval | None:
        item = self._by_task.get(task_id)
        if item is None:
            return None
        if item.expired(now):
            self._drop(item)
            return None
        return item

    def _earlier_pending(self, item: PendingApproval, now: float) -> PendingApproval | None:
        """同一任务链里比它先登记、且还没处理完的卡片（挡住它的那一条）。

        执行中、以及执行完但续跑还没结束的也算：后面的操作往往要用前面的结果。
        """
        for other in self._live(item.session_id, now):
            if other.task_id == item.task_id or other.seq >= item.seq:
                continue
            if other.chain != item.chain:
                continue
            if other.status in (ApprovalStatus.PENDING, ApprovalStatus.RUNNING):
                return other
            if (
                other.status == ApprovalStatus.DONE
                and not other.settled
                and now < other.settle_by
            ):
                return other
        return None

    def claim(self, task_id: str, operator: str) -> CardClaim:
        """卡片点了批准：按登记先后批准，占住后别人不能再点。点的人记在 ``operator``。"""
        now = time.time()
        with self._lock:
            item = self._lookup(task_id, now)
            if item is None:
                return CardClaim(ClaimReason.MISSING)
            if not operator:
                return CardClaim(ClaimReason.FORBIDDEN, item)
            if item.status != ApprovalStatus.PENDING:
                return CardClaim(ClaimReason.DUPLICATE, item)
            blocker = self._earlier_pending(item, now)
            if blocker is not None:
                return CardClaim(ClaimReason.BLOCKED, blocker)
            item.status = ApprovalStatus.RUNNING
            item.operator = operator
            # 从批准这一刻重新计有效期：卡片可能在快过期时才被点，执行期间若过期被清掉，
            # 结果就记不下来（续跑丢失、后面的卡片被提前放开）
            item.expires_at = max(item.expires_at, now + self._ttl)
            return CardClaim(ClaimReason.OK, item)

    def cancel(self, task_id: str, operator: str) -> CardClaim:
        """卡片点了拒绝：作废，留下一次性的续跑说明。已经点过的不动。

        拒绝不受先后顺序限制，也不挡后面的卡片。作废的登记保留到过期，只为交出续跑说明
        和让再次点击得到「已处理」；不会被复用、不参与防重放。
        """
        now = time.time()
        with self._lock:
            item = self._lookup(task_id, now)
            if item is None:
                return CardClaim(ClaimReason.MISSING)
            if not operator:
                return CardClaim(ClaimReason.FORBIDDEN, item)
            if item.status != ApprovalStatus.PENDING:
                return CardClaim(ClaimReason.DUPLICATE, item)
            item.status = ApprovalStatus.CANCELLED
            item.operator = operator
            item.continuation = rejection_note(item.summary)
            return CardClaim(ClaimReason.OK, item)

    def complete(
        self, task_id: str, *, result_text: str, result_status: str, continuation: str
    ) -> bool:
        """执行结束：记下结果与续跑说明，保留到过期（供回灌与防重放），不能再点。

        :returns: 是否记上了；卡片已不在确认簿里（被作废、进程内状态丢失）时为 False
        """
        now = time.time()
        with self._lock:
            item = self._by_task.get(task_id)
            if item is None:
                return False
            item.status = ApprovalStatus.DONE
            item.result_text = result_text
            item.result_status = result_status
            item.continuation = continuation
            item.settled = False
            item.settle_by = now + self._settle_timeout
            item.expires_at = max(now + self._ttl, item.settle_by)
            return True

    def settle(self, task_id: str) -> None:
        """续跑那一轮结束（或不需要续跑）：放开后面的卡片。结果仍保留到过期，供防重放。"""
        with self._lock:
            item = self._by_task.get(task_id)
            if item is not None:
                item.settled = True

    def executed(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        chain: str = "",
        identity: dict[str, Any] | None = None,
    ) -> PendingApproval | None:
        """同一任务链里已批准并**执行成功**、参数完全一致的那一条；用于续跑时防重放。

        只在续跑那条链里回放（``chain`` 取 ``approval_chain_var``，普通轮次为空即不回放）：
        用户之后在新的一轮里再要求做一次（再发一遍文件、重新请求同一个地址），
        应该重新出卡片、真的再执行，而不是拿到旧结果、以为已经做了。
        执行失败的不回放：再要求一次时同样重新出卡片。
        """
        if not chain:
            return None
        now = time.time()
        digest = arguments_digest(tool_name, identity if identity is not None else arguments)
        with self._lock:
            for item in self._live(session_id, now):
                if (
                    item.status == ApprovalStatus.DONE
                    and item.result_status == "success"
                    and item.digest == digest
                    and item.chain == chain
                ):
                    return item
        return None

    def take_continuation(self, session_id: str, task_id: str) -> PendingApproval | None:
        """取走续跑说明（一次性）。不是本会话的、还没处理完的、已取过的都返回 None。

        执行完（done）与被拒绝（cancelled）的都有续跑说明。同一任务链里还在等用户点的
        卡片会附在说明后面：续跑那一轮的模型不知道它们已经登记过，会换个写法再发起一次，
        用户就多看到一张重复的卡片。
        """
        now = time.time()
        with self._lock:
            item = self._lookup(task_id, now)
            if (
                item is None
                or item.session_id != session_id
                or item.status not in (ApprovalStatus.DONE, ApprovalStatus.CANCELLED)
                or not item.continuation
            ):
                return None
            taken = replace(item)
            item.continuation = ""
            waiting = sorted(
                (
                    other
                    for other in self._live(session_id, now)
                    if other.chain == item.chain
                    and other.task_id != item.task_id
                    and other.status == ApprovalStatus.PENDING
                ),
                key=lambda other: other.seq,
            )
            if waiting:
                taken.continuation += waiting_note(waiting)
            return taken

    def is_ready(self, task_id: str) -> bool:
        """这张卡片现在能不能点：还没处理，且同一任务链里前面没有挡着的。"""
        now = time.time()
        with self._lock:
            item = self._lookup(task_id, now)
            return (
                item is not None
                and item.status == ApprovalStatus.PENDING
                and self._earlier_pending(item, now) is None
            )

    def queued_behind(self, task_id: str) -> int:
        """同一任务链里排在这张后面、还没处理的卡片数。"""
        now = time.time()
        with self._lock:
            item = self._lookup(task_id, now)
            if item is None:
                return 0
            return sum(
                1
                for other in self._live(item.session_id, now)
                if other.chain == item.chain
                and other.seq > item.seq
                and other.status == ApprovalStatus.PENDING
            )

    def take_ready_cards(self, session_id: str) -> list[PendingApproval]:
        """可以发出去的卡片（企微用）：还没发过、前面没有挡着的，按登记先后。

        取出即标记为已发，并从发出时起重新计有效期——排队等了一阵的卡片，
        不能一发出来就只剩几分钟。
        """
        now = time.time()
        with self._lock:
            ready = [
                item
                for item in self._live(session_id, now)
                if item.status == ApprovalStatus.PENDING
                and not item.card_sent
                and self._earlier_pending(item, now) is None
            ]
            ready.sort(key=lambda item: item.seq)
            for item in ready:
                item.card_sent = True
                item.expires_at = max(item.expires_at, now + self._ttl)
            return ready

    def lookup(self, task_id: str) -> PendingApproval | None:
        """按卡片 id 取一条仍有效的登记。过期的会清掉。不改变状态。"""
        now = time.time()
        with self._lock:
            return self._lookup(task_id, now)

    def list_pending(self, session_id: str) -> list[PendingApproval]:
        """本会话还没点过的卡片（按登记先后）。刷新网页后靠它把按钮画回来。"""
        now = time.time()
        with self._lock:
            return sorted(
                (
                    item
                    for item in self._live(session_id, now)
                    if item.status == ApprovalStatus.PENDING
                ),
                key=lambda item: item.seq,
            )

    def finish(self, task_id: str) -> None:
        """执行失败等收尾：直接作废，避免卡片一直停在「执行中」。已 complete 的不动。"""
        with self._lock:
            item = self._by_task.get(task_id)
            if item is None or item.status == ApprovalStatus.DONE:
                return
            item.status = ApprovalStatus.DONE
            self._drop(item)
