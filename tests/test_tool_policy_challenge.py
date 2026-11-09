"""工具注册中心的策略执行：拒绝 / 需确认、批准不抵拒绝、续跑回放、任务链。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.react.models import CustomerScope
from csagent.tools.approvals import ApprovalStore, ClaimReason, approval_chain_var
from csagent.tools.policy import CHALLENGE, ToolPolicy
from csagent.tools.registry import ToolRegistry

from tests.approval_helpers import (
    DENY_POLICY,
    RecordingTool,
    challenge_registry,
    group_scope,
    make_agent,
    raise_card,
)

_DENY_THEN_CHALLENGE = """
policy:
  version: "1"
  default_risk: read
  tool_risks:
    send_file: external_effect
    read_file: read
  rules:
    - id: deny-debug
      effect: deny
      reason: 调试身份不能发文件
      when:
        tool: send_file
        auth_method: debug_token
    - id: group-send
      effect: challenge
      reason: 群里可见
      when:
        tool: send_file
        chat_type: group
"""


def _scope(**kwargs) -> CustomerScope:
    base = dict(session_id="s1", channel="wecom", user_id="u1", chat_type="single")
    base.update(kwargs)
    return CustomerScope(**base)


@pytest.mark.asyncio
async def test_policy_deny_before_challenge_and_card_does_not_run_the_tool():
    """规则按顺序命中；挂起只发卡片，工具不会先跑。"""
    tool = RecordingTool()
    registry = ToolRegistry(
        tools=[tool],
        permission_matrix={"agent": ["send_file"]},
        policy=ToolPolicy.from_text(_DENY_THEN_CHALLENGE, profile="product"),
        approvals=ApprovalStore(),
    )
    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        denied = await registry.execute_detailed(
            "send_file", {"path": "a.xlsx"}, _scope(auth_method="debug_token", chat_type="group"), "agent"
        )
        assert denied.status == "denied"
        assert denied.rule_id == "deny-debug"
        assert tool.calls == 0

        challenged = await registry.execute_detailed(
            "send_file", {"path": "a.xlsx"}, _scope(chat_type="group"), "agent"
        )
        assert challenged.status == "challenged"
        assert challenged.decision == CHALLENGE
        assert "task_" not in challenged.text
        buf = audit_trace_var.get()
        assert buf is not None and buf.challenges
        assert buf.challenges[-1]["task_id"].startswith("task_")
        assert "code" not in buf.challenges[-1]
        assert tool.calls == 0
        # 同一参数再调一次仍不执行，并复用同一张卡片
        again = await registry.execute_detailed(
            "send_file", {"path": "a.xlsx"}, _scope(chat_type="group"), "agent"
        )
        assert again.status == "challenged"
        assert buf.challenges[-1]["task_id"] == buf.challenges[0]["task_id"]
        assert tool.calls == 0
    finally:
        audit_trace_var.reset(token)


@pytest.mark.asyncio
async def test_approval_does_not_override_a_later_deny() -> None:
    tool = RecordingTool()
    store = ApprovalStore()
    pending = await raise_card(challenge_registry(tool, store=store), group_scope())
    # 卡片发出后策略改成拒绝（重启换了策略）：批准也不再执行
    denying = challenge_registry(tool, store=store, policy=DENY_POLICY)
    outcome = await denying.execute_approved(
        pending.tool_name, pending.arguments, group_scope(), "agent", rule_id=pending.rule_id
    )
    assert outcome.status == "denied"
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_continuation_turn_replays_result_instead_of_running_again() -> None:
    tool = RecordingTool(result="已发送")
    registry = challenge_registry(tool)
    store = registry.approvals
    pending = await raise_card(registry, group_scope(trace_id="t-orig"))
    assert pending.chain == "t-orig"
    store.claim(pending.task_id, "owner")
    await make_agent(registry).execute_approved_action(pending)
    assert tool.calls == 1

    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    # 续跑那一轮沿用原任务的链（渠道设 approval_chain_var）
    chain_token = approval_chain_var.set(pending.chain)
    try:
        replay = await registry.execute_detailed(
            "send_file", {"path": "a.xlsx"}, group_scope(), "agent"
        )
        other = await registry.execute_detailed(
            "send_file", {"path": "b.xlsx"}, group_scope(), "agent"
        )
    finally:
        approval_chain_var.reset(chain_token)
        audit_trace_var.reset(token)
    assert replay.status == "success" and replay.decision == "approved_replay"
    assert "已发送" in replay.text
    assert tool.calls == 1  # 没有重复执行，也没有再出卡片
    # 参数变了就是另一项操作，照常要确认
    assert other.status == "challenged"
    assert [c["summary"].split()[1] for c in buf.challenges] == ["b.xlsx"]


@pytest.mark.asyncio
async def test_new_turn_with_same_arguments_asks_again_instead_of_replaying() -> None:
    """续跑之外（用户新的一轮里说「再发一次」）：同参数也要重新出卡片、真的再执行。"""
    tool = RecordingTool(result="已发送")
    registry = challenge_registry(tool)
    store = registry.approvals
    pending = await raise_card(registry, group_scope(trace_id="t-orig"))
    store.claim(pending.task_id, "owner")
    await make_agent(registry).execute_approved_action(pending)
    store.settle(pending.task_id)

    again = await registry.execute_detailed(
        "send_file", {"path": "a.xlsx"}, group_scope(), "agent"
    )
    assert again.status == "challenged"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_repeated_call_in_one_turn_yields_one_card_per_distinct_action() -> None:
    """同一操作重试只出一张卡；参数不同的操作各出一张，互不影响。"""
    registry = challenge_registry()
    buf = AuditTraceBuffer()
    token = audit_trace_var.set(buf)
    try:
        for path in ("a.xlsx", "a.xlsx", "b.xlsx"):
            await registry.execute_detailed("send_file", {"path": path}, group_scope(), "agent")
    finally:
        audit_trace_var.reset(token)
    assert [c["summary"].split()[1] for c in buf.challenges] == ["a.xlsx", "b.xlsx"]
    assert len({c["task_id"] for c in buf.challenges}) == 2


@pytest.mark.asyncio
async def test_parallel_tasks_in_one_session_do_not_block_each_other() -> None:
    """同一会话里并行的两个任务各出一张卡：互不依赖，谁先批都行。"""
    registry = challenge_registry()
    store = registry.approvals
    a = await raise_card(registry, group_scope(trace_id="turn-1"), "a.xlsx")
    b = await raise_card(registry, group_scope(trace_id="turn-2"), "b.xlsx")
    # 同一任务里后出的卡片照样排在后面
    c = await raise_card(registry, group_scope(trace_id="turn-1"), "c.xlsx")
    assert a.chain == c.chain != b.chain
    assert store.claim(b.task_id, "owner").reason == ClaimReason.OK
    assert store.claim(c.task_id, "owner").reason == ClaimReason.BLOCKED


@pytest.mark.asyncio
async def test_model_is_told_when_its_card_is_queued_behind_another() -> None:
    registry = challenge_registry()
    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        first = await registry.execute_detailed("send_file", {"path": "a"}, group_scope(trace_id="t"), "agent")
        second = await registry.execute_detailed("send_file", {"path": "b"}, group_scope(trace_id="t"), "agent")
    finally:
        audit_trace_var.reset(token)
    assert "前面还有待确认的操作" not in first.text
    assert "前面还有待确认的操作" in second.text


def _real_policy(*, challenge_on: bool = False) -> ToolPolicy:
    """仓库里的 policy.yaml；challenge_on 时按「总开关已打开」读，验证规则本身写得对不对。"""
    root = Path(__file__).resolve().parents[1]
    text = (root / "configs" / "policy.yaml").read_text(encoding="utf-8")
    if challenge_on:
        text = text.replace("challenge_enabled: false", "challenge_enabled: true")
    return ToolPolicy.from_text(text)


def test_dynamically_registered_mcp_tools_need_confirmation():
    """MCP 远端工具注册成 mcp_<服务>_<工具>，名字事先不知道，也要出卡片。"""
    policy = _real_policy(challenge_on=True)
    scope = _scope(chat_type="group")
    discovered = policy.decide("mcp_crm_delete_customer", {}, scope)
    assert discovered.effect == CHALLENGE and discovered.rule_id == "mcp-call"
    assert discovered.risk == "external_effect"
    assert policy.decide("mcp_call_tool", {}, scope).effect == CHALLENGE
    listing = policy.decide("mcp_list_tools", {}, scope)
    assert listing.effect == "allow" and listing.risk == "read"


def test_every_configured_tool_has_a_risk_level():
    """tool_packs.yaml 里声明的工具都在 policy.yaml 里归了类；漏配的会落到 default_risk。"""
    root = Path(__file__).resolve().parents[1]
    packs = yaml.safe_load((root / "configs" / "tool_packs.yaml").read_text(encoding="utf-8"))
    names = {
        tool["name"]
        for pack in (packs.get("tool_packs") or {}).values()
        for tool in (pack.get("tools") or [])
        if isinstance(tool, dict) and tool.get("name")
    }
    assert names, "tool_packs.yaml 里没读到任何工具"
    assert _real_policy().unclassified(names) == []


def test_challenge_switch_off_allows_but_keeps_later_deny():
    """总开关关掉：确认规则当作不存在，后面的 deny 照样拦；放行时记下本该弹卡片的规则。"""
    policy = ToolPolicy.from_text(_DENY_THEN_CHALLENGE.replace("  rules:", "  challenge_enabled: false\n  rules:") + """
    - id: deny-after
      effect: deny
      when:
        tool: send_file
        user_id_prefix: blocked
""")
    in_group = policy.decide("send_file", {}, _scope(chat_type="group"))
    assert (in_group.effect, in_group.rule_id) == ("allow", "group-send")
    blocked = policy.decide("send_file", {}, _scope(chat_type="group", user_id="blocked-1"))
    assert (blocked.effect, blocked.rule_id) == ("deny", "deny-after")
    assert policy.decide("read_file", {}, _scope()).rule_id == ""


def test_challenge_switch_defaults_to_on():
    policy = ToolPolicy.from_text(_DENY_THEN_CHALLENGE)
    assert policy.challenge_enabled is True
    assert policy.decide("send_file", {}, _scope(chat_type="group")).effect == CHALLENGE


def test_real_policy_keeps_confirmations_off_for_now():
    """线上体验不变：群里发文件、改回复风格都直接放行。要开启确认卡片时改 policy.yaml 并更新这条。"""
    policy = _real_policy()
    assert policy.challenge_enabled is False
    assert policy.decide("send_file", {}, _scope(chat_type="group")).effect == "allow"
    assert policy.decide("set_reply_style", {}, _scope()).effect == "allow"


def test_tool_prefix_rule_and_prefix_risk_from_text():
    policy = ToolPolicy.from_text("""
policy:
  tool_risks:
    "x_*": code_exec
    x_safe: read
  rules:
    - id: x-any
      effect: deny
      when: {tool_prefix: x_}
""")
    assert policy.risk_of("x_run") == "code_exec"
    assert policy.risk_of("x_safe") == "read"  # 精确名优先于前缀
    assert policy.decide("x_run", {}, _scope()).effect == "deny"
    assert policy.decide("y_run", {}, _scope()).effect == "allow"
    assert policy.unclassified(["x_run", "x_safe", "y_run"]) == ["y_run"]


@pytest.mark.asyncio
async def test_same_file_written_differently_reuses_the_waiting_card(tmp_path: Path):
    """续跑里模型把 outbound/a.txt 写成完整路径再发一次：仍是那张卡，不多出第二张。"""
    from csagent.tools.packs.workspace_files_pack import SendFileTool

    scope = group_scope()
    tool = SendFileTool(root=tmp_path)
    target = tool.approval_identity({"path": "outbound/a.txt"}, scope)["path"]
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text("x", encoding="utf-8")
    full = Path(target).relative_to(tmp_path).as_posix()
    assert full.startswith("data/uploads/") and full != "outbound/a.txt"

    registry = challenge_registry(tool)
    first = await raise_card(registry, scope, path="outbound/a.txt")
    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        again = await registry.execute_detailed("send_file", {"path": full}, scope, "agent")
        buf = audit_trace_var.get()
        assert buf is not None and not buf.challenges  # 不再下发一张卡片
    finally:
        audit_trace_var.reset(token)

    assert again.status == "challenged"
    assert "此前已发出确认卡片" in again.text
    assert [item.task_id for item in registry.approvals.list_pending(scope.session_id)] == [
        first.task_id
    ]


@pytest.mark.asyncio
async def test_identity_failure_falls_back_to_raw_arguments():
    """工具规整参数时出错，确认流程照常（按原参数判断）。"""

    class Broken(RecordingTool):
        def approval_identity(self, arguments, scope):
            raise RuntimeError("boom")

    registry = challenge_registry(Broken())
    card = await raise_card(registry, group_scope())
    assert card.arguments == {"path": "a.xlsx"}
