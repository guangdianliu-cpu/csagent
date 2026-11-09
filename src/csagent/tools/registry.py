"""工具注册中心与权限校验（工具调用的策略执行点）。

所有工具调用都经过 :meth:`ToolRegistry.execute_detailed`，判定顺序：
Agent 权限矩阵 → 本轮委托范围（Skill allowed-tools）→ 策略（configs/policy.yaml，
allow / deny / challenge）→ 执行 → 结果过滤（遮掉已知密钥值）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from csagent.audit.trace_buffer import append_audit_challenge
from csagent.react.models import CustomerScope
from csagent.tools.approvals import ApprovalStore, approval_chain_var
from csagent.tools.base import BaseTool, ToolCallError
from csagent.tools.delegation import current_delegation, delegation_scope_var, restrict_tools
from csagent.tools.policy import CHALLENGE, DENY, PolicyDecision, ToolPolicy

logger = structlog.get_logger(__name__)

# 始终允许的工具（即使白名单开启也不拦截）
_ALWAYS_ALLOWED = frozenset({
    "list_skills", "load_skill",
})

# 单工具兜底超时默认值（秒）。装配层会用 settings.tool_timeout_seconds 覆盖。
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0


@dataclass
class ToolOutcome:
    """一次工具调用的结果与判定（handlers 据此写审计）。

    ``status``：success | error | denied | challenged | rejected；
    ``decision``：allow | deny | challenge | approved（用户确认后放行）。
    """

    text: str
    status: str = "success"
    decision: str = "allow"
    rule_id: str = ""
    risk: str = ""


class ToolRegistry:
    """工具注册与按 Agent 权限执行。

    判定顺序：
    1. Agent 权限矩阵（静态）
    2. 本轮委托范围（Skill allowed-tools，动态）
    3. 策略（configs/policy.yaml：allow / deny / challenge）
    """

    def __init__(
        self,
        tools: list[BaseTool],
        permission_matrix: dict[str, list[str]],
        *,
        default_timeout_seconds: float | None = DEFAULT_TOOL_TIMEOUT_SECONDS,
        result_filter: Callable[[str], str] | None = None,
        policy: ToolPolicy | None = None,
        approvals: ApprovalStore | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._permission_matrix = permission_matrix
        self._default_timeout = default_timeout_seconds
        # 工具结果进模型上下文前的过滤（装配时挂密钥遮挡）。所有工具、所有返回路径
        # （含超时与异常文案）都经过它——这里是工具结果离开执行侧的唯一出口。
        self._result_filter = result_filter
        # 策略决策点与确认卡片登记簿；都为 None 时行为与改造前一致（只看权限矩阵与白名单）
        self._policy = policy
        self.approvals = approvals

    @classmethod
    def from_tools_and_agent(
        cls,
        tools: list[BaseTool],
        agent_name: str,
        *,
        default_timeout_seconds: float | None = DEFAULT_TOOL_TIMEOUT_SECONDS,
        result_filter: Callable[[str], str] | None = None,
        policy: ToolPolicy | None = None,
        approvals: ApprovalStore | None = None,
    ) -> "ToolRegistry":
        """从工具列表创建，并赋予指定 Agent 全部调用权限。"""
        tool_names = [t.name for t in tools]
        return cls(
            tools=tools,
            permission_matrix={agent_name: tool_names},
            default_timeout_seconds=default_timeout_seconds,
            result_filter=result_filter,
            policy=policy,
            approvals=approvals,
        )

    @property
    def policy(self) -> ToolPolicy | None:
        return self._policy

    def list_tools_for_agent(self, agent_name: str) -> list[BaseTool]:
        """返回某 Agent 可用的工具列表。"""
        allowed = set(self._permission_matrix.get(agent_name, []))
        return [tool for name, tool in self._tools.items() if name in allowed]

    def list_tool_schemas_for_agent(self, agent_name: str) -> list[dict[str, Any]]:
        """返回某 Agent 可用工具的 OpenAI function calling schema 列表。

        格式与 OpenAI chat/completions API 的 tools 参数兼容：
        [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        """
        tools = self.list_tools_for_agent(agent_name)
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
            }
            for tool in tools
        ]

    def _timeout_for(self, tool: BaseTool) -> float | None:
        """取单工具超时：工具自己声明的优先，否则用注册中心默认值。"""
        own = getattr(tool, "timeout_seconds", None)
        if own is not None:
            try:
                own = float(own)
            except (TypeError, ValueError):
                return self._default_timeout
            return own if own > 0 else None
        return self._default_timeout

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        agent_name: str,
        *,
        timeout: float | None = None,
    ) -> str:
        """执行工具，只返回结果文本（兼容旧调用方）；要判定结果用 :meth:`execute_detailed`。"""
        outcome = await self.execute_detailed(
            tool_name, arguments, scope, agent_name, timeout=timeout
        )
        return outcome.text

    async def execute_detailed(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        agent_name: str,
        *,
        timeout: float | None = None,
    ) -> ToolOutcome:
        """执行工具，含权限校验、策略判定、单工具超时与异常转换。

        拒绝与待确认都回一句可读文本让循环继续，但 ``status`` 如实标成
        denied / challenged——以前一律返回字符串，审计里全记成 success。

        超时按「调用方指定 > 工具自己声明 > 注册中心默认」取值，超时只让**这一个**
        工具失败并回一句可读错误，循环继续跑下一步——不像整体超时那样把整轮打断。
        改造前这里是裸 await：一个工具卡住 170 秒，后续所有操作只剩 10 秒。
        """
        outcome = await self._execute(tool_name, arguments, scope, agent_name, timeout=timeout)
        if self._result_filter is not None and outcome.text:
            outcome.text = self._result_filter(outcome.text)
        return outcome

    async def _execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        agent_name: str,
        *,
        timeout: float | None = None,
    ) -> ToolOutcome:
        risk = self._policy.risk_of(tool_name) if self._policy is not None else ""

        # 1. Agent 权限矩阵
        allowed = self._permission_matrix.get(agent_name, [])
        if tool_name not in allowed:
            return ToolOutcome(
                f"权限错误: 领域 Agent '{agent_name}' 无权调用工具 '{tool_name}'",
                status="denied", decision=DENY, rule_id="agent_permission", risk=risk,
            )

        # 2. 本轮委托范围（Skill allowed-tools 硬约束）
        whitelist = session_tool_whitelist()
        if whitelist is not None and tool_name not in whitelist and tool_name not in _ALWAYS_ALLOWED:
            return ToolOutcome(
                f"权限错误: 当前 Skill 会话限制工具集，'{tool_name}' 不在允许列表中。"
                f"允许的工具: {', '.join(sorted(whitelist))}",
                status="denied", decision=DENY, rule_id="skill_allowed_tools", risk=risk,
            )

        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolOutcome(
                f"权限错误: 未知工具 '{tool_name}'", status="rejected", risk=risk
            )

        # 3. 策略
        decision = (
            self._policy.decide(tool_name, arguments, scope)
            if self._policy is not None
            else PolicyDecision()
        )
        verdict = decision.effect
        if decision.effect == DENY:
            logger.warning("tool_policy_denied", tool=tool_name, rule=decision.rule_id)
            reason = f"：{decision.reason}" if decision.reason else ""
            return ToolOutcome(
                f"权限错误: 策略不允许调用 {tool_name}（规则 {decision.rule_id}）{reason}。"
                "不要重试同一调用；换一种做法，或直接告诉用户这件事做不到。",
                status="denied", decision=DENY, rule_id=decision.rule_id, risk=decision.risk,
            )
        if decision.effect == CHALLENGE:
            identity = _approval_identity(tool, arguments, scope)
            # 续跑时模型按原参数又调一次已批准执行过的操作：回放结果，不重复执行、不再要确认
            done = (
                self.approvals.executed(
                    scope.session_id,
                    tool_name,
                    arguments,
                    chain=approval_chain_var.get(),
                    identity=identity,
                )
                if self.approvals is not None
                else None
            )
            if done is not None:
                return ToolOutcome(
                    "（该操作已在用户批准后执行过，不要再调用）执行结果：\n" + done.result_text,
                    status="success", decision="approved_replay",
                    rule_id=decision.rule_id, risk=decision.risk,
                )
            return self._challenge(
                tool_name, arguments, scope, decision, agent_name=agent_name, identity=identity
            )

        text, status = await self._invoke(tool, tool_name, arguments, scope, agent_name, timeout)
        return ToolOutcome(
            text, status=status, decision=verdict, rule_id=decision.rule_id, risk=decision.risk
        )

    async def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        agent_name: str,
        *,
        rule_id: str = "",
    ) -> ToolOutcome:
        """卡片已批准：按保存的参数执行一次，不再走 challenge。

        技能白名单是上一轮的委托范围，点卡片时那一轮已经结束，这里不再套用，
        否则已批准的调用会因为白名单清空而被拒绝。权限矩阵仍然生效。
        """
        risk = self._policy.risk_of(tool_name) if self._policy is not None else ""
        allowed = self._permission_matrix.get(agent_name, [])
        if tool_name not in allowed:
            return ToolOutcome(
                f"权限错误: 领域 Agent '{agent_name}' 无权调用工具 '{tool_name}'",
                status="denied", decision=DENY, rule_id="agent_permission", risk=risk,
            )
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolOutcome(
                f"权限错误: 未知工具 '{tool_name}'", status="rejected", risk=risk
            )
        # 批准只抵掉 challenge，不抵 deny：卡片发出后策略若改成拒绝（或主体变了），仍按拒绝办
        if self._policy is not None:
            decision = self._policy.decide(tool_name, arguments, scope)
            if decision.effect == DENY:
                logger.warning("tool_policy_denied_after_approval", tool=tool_name, rule=decision.rule_id)
                return ToolOutcome(
                    f"权限错误: 策略不允许调用 {tool_name}（规则 {decision.rule_id}），已批准的操作也不再执行。",
                    status="denied", decision=DENY, rule_id=decision.rule_id, risk=decision.risk,
                )
        # timeout=None → _invoke 回落到工具 / 注册中心默认超时，与正常调用同一口径
        text, status = await self._invoke(
            tool, tool_name, arguments, scope, agent_name, timeout=None
        )
        outcome = ToolOutcome(
            text, status=status, decision="approved", rule_id=rule_id, risk=risk
        )
        if self._result_filter is not None and outcome.text:
            outcome.text = self._result_filter(outcome.text)
        return outcome

    def _challenge(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        decision: PolicyDecision,
        *,
        agent_name: str,
        identity: dict[str, Any],
    ) -> ToolOutcome:
        """登记待确认操作，并让本轮停在卡片上。模型看不到卡片编号。"""
        if self.approvals is None:
            # 没有确认通道就无从确认，只能按拒绝处理
            return ToolOutcome(
                f"权限错误: {tool_name} 需要用户确认，但当前部署没有确认通道，已拒绝。",
                status="denied", decision=DENY, rule_id=decision.rule_id, risk=decision.risk,
            )
        waiting = self.approvals.pending_for(scope.session_id, tool_name, identity)
        if waiting is not None:
            # 同一项操作（可能换了参数写法）此前已发出卡片、还在等：不再发一张，
            # 告诉模型它在等，免得用户看到两张、两张都批准就执行两次
            logger.info("tool_policy_challenge_reused", tool=tool_name, task_id=waiting.task_id)
            return ToolOutcome(
                f"已暂停：这项操作此前已发出确认卡片（{waiting.summary}），正在等用户确认。"
                "不要再调用它（换个参数写法也不行），也不要说它已经完成；"
                "用户批准后系统会执行它，并把结果交给你。在回复里提一句仍在等用户确认即可。",
                status="challenged", decision=CHALLENGE, rule_id=decision.rule_id, risk=decision.risk,
            )
        summary = describe_action(tool_name, arguments, scope)
        pending = self.approvals.request(
            scope.session_id,
            tool_name,
            arguments,
            summary=summary,
            rule_id=decision.rule_id,
            user_id=scope.user_id,
            auth_method=scope.auth_method,
            channel=scope.channel,
            chat_type=scope.chat_type,
            chat_id=scope.chat_id,
            agent_name=agent_name,
            reason=decision.reason,
            chain=approval_chain_var.get() or scope.trace_id,
            identity=identity,
        )
        append_audit_challenge({
            "task_id": pending.task_id,
            "tool": tool_name,
            "summary": summary,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
            "expires_at": pending.expires_at,
            "user_id": scope.user_id,
            "chat_type": scope.chat_type,
        })
        logger.info("tool_policy_challenge", tool=tool_name, rule=decision.rule_id)
        reason = f"（{decision.reason}）" if decision.reason else ""
        queued = (
            "" if self.approvals.is_ready(pending.task_id)
            else "前面还有待确认的操作，用户要先处理完那一项，才能确认这一项。"
        )
        return ToolOutcome(
            f"已暂停：{tool_name} 需要用户确认后才能执行{reason}。{queued}"
            "系统已向用户发出确认卡片。本轮不要再调用它，也不要替用户确认；"
            "依赖它结果的后续步骤先不要做——用户批准后系统会执行它，并把结果交给你继续完成原任务。"
            "在回复里简要说明正在等用户点卡片即可。",
            status="challenged", decision=CHALLENGE, rule_id=decision.rule_id, risk=decision.risk,
        )

    async def _invoke(
        self,
        tool: BaseTool,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        agent_name: str,
        timeout: float | None,
    ) -> tuple[str, str]:
        """执行工具本体，返回 ``(结果文本, success | error)``。"""
        effective_timeout = timeout if timeout is not None else self._timeout_for(tool)
        try:
            if effective_timeout is None:
                return await tool.execute(arguments, scope), "success"
            result = await asyncio.wait_for(
                tool.execute(arguments, scope), timeout=effective_timeout
            )
            return result, "success"
        except asyncio.TimeoutError:
            # 必须排在 except Exception 前面：Python 3.11+ 起 asyncio.TimeoutError
            # 就是内置 TimeoutError（OSError 子类），会被通用兜底吞成「执行失败」。
            logger.warning(
                "tool_call_timeout",
                tool=tool_name,
                agent_name=agent_name,
                timeout_seconds=effective_timeout,
            )
            return (
                f"工具 {tool_name} 执行超时（{effective_timeout:g}s，已取消）。"
                "请检查参数是否过宽，或改用其他方式获取信息；不要原样重试同一调用。"
            ), "error"
        except ToolCallError as exc:
            return exc.text, "error"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tool_call_failed",
                tool=tool_name,
                agent_name=agent_name,
                error=str(exc),
            )
            return f"工具 {tool_name} 执行失败: {exc}", "error"


def _approval_identity(
    tool: BaseTool, arguments: dict[str, Any], scope: CustomerScope
) -> dict[str, Any]:
    """工具规整过的参数（判断是不是同一张卡片用）；工具自己出错时退回原参数。"""
    try:
        identity = tool.approval_identity(arguments, scope)
    except Exception as exc:  # noqa: BLE001 - 规整失败不能挡住确认流程
        logger.warning("approval_identity_failed", tool=getattr(tool, "name", ""), error=str(exc))
        return arguments
    return identity if isinstance(identity, dict) else arguments


# 卡片和提示里给用户看的回复风格名称
_REPLY_STYLE_LABELS = {"concise": "简洁", "detailed": "详细", "normal": "默认"}
# 卡片上回复偏好备注最多显示的字数（企微卡片引用区上限 120 字，留给前面的说明）
_NOTE_PREVIEW_CHARS = 80


def describe_action(tool_name: str, arguments: dict[str, Any], scope: CustomerScope) -> str:
    """给用户看的操作说明（系统生成，不经模型转述）。"""
    if tool_name == "send_file":
        target = "群聊（群里所有成员可见）" if scope.chat_type == "group" else "当前对话"
        return f"把文件 {arguments.get('path', '')} 发送到{target}"
    if tool_name == "set_reply_style":
        # 备注批准后每轮都会作为「必须遵守」的指令注入，卡片上必须让人看见；
        # 太长时省略显示（企微卡片引用区放不下），完整内容以审计里的参数为准
        style = str(arguments.get("style") or "")
        label = _REPLY_STYLE_LABELS.get(style)
        note = str(arguments.get("note") or "").strip()
        if len(note) > _NOTE_PREVIEW_CHARS:
            note = note[: _NOTE_PREVIEW_CHARS - 1] + "…"
        if label:
            return f"把回复风格改成「{label}」" + (f"，备注：{note}" if note else "")
        return f"把回复风格改成：{note}" if note else "修改回复风格"
    preview = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(preview) > 200:
        preview = preview[:199] + "…"
    return f"调用 {tool_name}，参数 {preview}"


def apply_skill_whitelist(allowed_tools: list[str]) -> None:
    """应用 Skill 的 allowed-tools 到本轮白名单（并集模式，见 tools/delegation.py）。

    多个 Skill 的 allowed-tools 取并集，避免把自己锁死。
    """
    restrict_tools(allowed_tools)


def session_tool_whitelist() -> set[str] | None:
    """本轮生效的工具白名单；None = 不限制。"""
    scope = current_delegation()
    return None if scope is None else scope.allowed_tools


def clear_skill_whitelist() -> None:
    """清除本轮委托范围。"""
    delegation_scope_var.set(None)
