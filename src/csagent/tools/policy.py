"""工具调用策略：确定性地判定「此刻允许做什么」（策略决策点）。

模型只提出「想调哪个工具、带什么参数」；能不能执行由这里按 ``configs/policy.yaml`` 判，
结果三态：

- ``allow``：放行；
- ``deny``：拒绝，回给模型一句可读原因，让它换路径而不是反复重试；
- ``challenge``：需要用户确认。系统向用户发确认卡片，模型只收到「已暂停，等待确认」；
  用户在卡片上批准后，按登记时保存的参数直接执行一次（见 tools/approvals.py）。

规则按顺序匹配，第一条命中的生效；都不命中就放行。每次判定带上命中的规则 id 与
策略文件的版本 + 内容摘要，写进会话审计——规则改过之后，仍能回答「当时按哪版规则放行的」。

``challenge_enabled: false`` 时整体关掉确认卡片：challenge 规则当作不存在，接着往下匹配
（后面的 deny 照样生效）；最后放行时仍记下命中的确认规则 id，开启前能估算会弹多少卡片。

判定只看确定性的输入：工具名（或名字前缀）、风险等级、通道、单聊 / 群聊、认证方式、
部署形态、参数（正则）。不看模型给的任何解释性文字。

动态注册的工具（MCP 远端工具注册成 ``mcp_<服务>_<工具>``）名字事先不知道：规则用
``tool_prefix`` 按前缀匹配，风险等级在 ``tool_risks`` 里用 ``前缀*`` 配置。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from csagent.infra.config import CONFIGS_DIR, active_profile
from csagent.react.models import CustomerScope

logger = structlog.get_logger(__name__)

ALLOW = "allow"
DENY = "deny"
CHALLENGE = "challenge"
_EFFECTS = (ALLOW, DENY, CHALLENGE)

# 风险等级（只是标签，放不放行由规则决定）
RISK_LEVELS = ("read", "session_write", "code_exec", "external_effect")


@dataclass(frozen=True)
class PolicyDecision:
    """一次判定的结果。"""

    effect: str = ALLOW
    rule_id: str = ""
    reason: str = ""
    risk: str = "read"


def _as_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return frozenset(str(v) for v in items)


@dataclass
class PolicyRule:
    """一条规则：``when`` 里的条件全部满足才命中；没写的条件不限。"""

    id: str
    effect: str
    reason: str = ""
    tools: frozenset[str] | None = None
    tool_prefixes: tuple[str, ...] = ()
    risks: frozenset[str] | None = None
    channels: frozenset[str] | None = None
    chat_types: frozenset[str] | None = None
    auth_methods: frozenset[str] | None = None
    profiles: frozenset[str] | None = None
    user_id_prefix: str = ""
    args: dict[str, re.Pattern[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> PolicyRule:
        when = data.get("when") or {}
        effect = str(data.get("effect") or "").strip().lower()
        if effect not in _EFFECTS:
            raise ValueError(f"策略规则 #{index} 的 effect 必须是 {'/'.join(_EFFECTS)}：{effect!r}")
        args: dict[str, re.Pattern[str]] = {}
        for name, pattern in (when.get("args") or {}).items():
            args[str(name)] = re.compile(str(pattern))
        return cls(
            id=str(data.get("id") or f"rule-{index}"),
            effect=effect,
            reason=str(data.get("reason") or "").strip(),
            tools=_as_set(when.get("tool")),
            tool_prefixes=tuple(sorted(_as_set(when.get("tool_prefix")) or ())),
            risks=_as_set(when.get("risk")),
            channels=_as_set(when.get("channel")),
            chat_types=_as_set(when.get("chat_type")),
            auth_methods=_as_set(when.get("auth_method")),
            profiles=_as_set(when.get("profile")),
            user_id_prefix=str(when.get("user_id_prefix") or ""),
            args=args,
        )

    def matches(
        self,
        tool_name: str,
        risk: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
        profile: str,
    ) -> bool:
        checks = (
            (self.tools, tool_name),
            (self.risks, risk),
            (self.channels, scope.channel),
            (self.chat_types, scope.chat_type),
            (self.auth_methods, scope.auth_method),
            (self.profiles, profile),
        )
        if any(allowed is not None and value not in allowed for allowed, value in checks):
            return False
        if self.tool_prefixes and not tool_name.startswith(self.tool_prefixes):
            return False
        if self.user_id_prefix and not scope.user_id.startswith(self.user_id_prefix):
            return False
        for name, pattern in self.args.items():
            if not pattern.search(str(arguments.get(name, ""))):
                return False
        return True


class ToolPolicy:
    """策略决策点。没有规则时恒放行（向后兼容）。"""

    def __init__(
        self,
        rules: list[PolicyRule] | None = None,
        *,
        tool_risks: dict[str, str] | None = None,
        default_risk: str = "read",
        version: str = "",
        digest: str = "",
        profile: str | None = None,
        challenge_enabled: bool = True,
    ) -> None:
        self._rules = rules or []
        self.challenge_enabled = challenge_enabled
        # 精确名字与「前缀*」分开存：精确优先，前缀取最长匹配
        risks = dict(tool_risks or {})
        self._tool_risks = {k: v for k, v in risks.items() if not k.endswith("*")}
        self._prefix_risks = sorted(
            ((k[:-1], v) for k, v in risks.items() if k.endswith("*")),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._default_risk = default_risk
        self.version = version
        self.digest = digest
        self._profile = profile

    @classmethod
    def from_file(cls, path: Path | None = None) -> ToolPolicy:
        """读 ``configs/policy.yaml``；文件不存在时返回恒放行的空策略。"""
        cfg_path = path or (CONFIGS_DIR / "policy.yaml")
        if not cfg_path.is_file():
            logger.warning("tool_policy_missing", path=str(cfg_path))
            return cls()
        text = cfg_path.read_text(encoding="utf-8")
        # 形态在进程内不变，加载时取一次，免得每次判定都去读 .env
        return cls.from_text(text, profile=active_profile())

    @classmethod
    def from_text(cls, text: str, *, profile: str | None = None) -> ToolPolicy:
        raw = (yaml.safe_load(text) or {}).get("policy") or {}
        rules = [
            PolicyRule.from_dict(item, index)
            for index, item in enumerate(raw.get("rules") or [], start=1)
        ]
        risks = {str(k): str(v) for k, v in (raw.get("tool_risks") or {}).items()}
        for tool, risk in risks.items():
            if risk not in RISK_LEVELS:
                raise ValueError(f"工具 {tool} 的风险等级 {risk!r} 不在 {RISK_LEVELS} 内")
        policy = cls(
            rules,
            tool_risks=risks,
            default_risk=str(raw.get("default_risk") or "read"),
            version=str(raw.get("version") or ""),
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            profile=profile,
            challenge_enabled=bool(raw.get("challenge_enabled", True)),
        )
        logger.info(
            "tool_policy_loaded",
            version=policy.version,
            digest=policy.digest,
            rules=[r.id for r in rules],
            challenge_enabled=policy.challenge_enabled,
        )
        return policy

    @property
    def label(self) -> str:
        """``版本@摘要``，写进审计。"""
        return f"{self.version or '-'}@{self.digest or '-'}"

    def risk_of(self, tool_name: str) -> str:
        if tool_name in self._tool_risks:
            return self._tool_risks[tool_name]
        for prefix, risk in self._prefix_risks:
            if tool_name.startswith(prefix):
                return risk
        return self._default_risk

    def unclassified(self, tool_names: list[str] | set[str]) -> list[str]:
        """没有在 ``tool_risks`` 里配置风险等级的工具（精确名或前缀都没命中）。

        这些工具落到 ``default_risk``；新挂的工具包、新接的 MCP 服务忘了归类时，
        启动时据此报出来，免得高风险工具被当成只读、绕过按风险写的规则。
        """
        return sorted(
            name
            for name in tool_names
            if name not in self._tool_risks
            and not any(name.startswith(prefix) for prefix, _risk in self._prefix_risks)
        )

    def decide(
        self, tool_name: str, arguments: dict[str, Any], scope: CustomerScope
    ) -> PolicyDecision:
        risk = self.risk_of(tool_name)
        profile = self._profile or ""
        skipped = ""
        for rule in self._rules:
            if not rule.matches(tool_name, risk, arguments, scope, profile):
                continue
            if rule.effect == CHALLENGE and not self.challenge_enabled:
                skipped = skipped or rule.id
                continue
            return PolicyDecision(
                effect=rule.effect, rule_id=rule.id, reason=rule.reason, risk=risk
            )
        return PolicyDecision(effect=ALLOW, rule_id=skipped, risk=risk)
