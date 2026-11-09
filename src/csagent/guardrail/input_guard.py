"""输入侧 Guardrail：配置驱动的规则引擎（M2.1）。

`InputGuard()`（无规则）保持向后兼容：恒通过，用于未接入配置的场景/单测骨架。
生产环境请用 `InputGuard.from_config(...)` 从 `configs/guardrail.yaml` 构建。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from csagent.guardrail.rules import InputRule

logger = structlog.get_logger(__name__)


class GuardrailVerdict(BaseModel):
    """Guardrail 审查结果。"""

    passed: bool
    violations: list[str] = Field(default_factory=list)


class InputGuard:
    """输入审查入口；无规则时恒通过（向后兼容对内放行场景）。"""

    def __init__(self, rules: list[InputRule] | None = None) -> None:
        self._rules = rules or []

    @classmethod
    def from_config(cls, rules_config: list[dict] | None) -> InputGuard:
        """从 `guardrail.yaml` 的 `input_rules` 列表构建。"""
        rules = [InputRule.from_dict(item) for item in (rules_config or [])]
        return cls(rules)

    async def review(self, text: str) -> GuardrailVerdict:
        violations: list[str] = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            result = rule.check(text or "")
            if not result.triggered:
                continue
            if rule.action == "block":
                violations.append(result.description)
            elif rule.action == "warn":
                logger.warning("input_guard_warn", rule=rule.name, detail=result.description)
            elif rule.action == "log":
                logger.info("input_guard_log", rule=rule.name, detail=result.description)
        return GuardrailVerdict(passed=len(violations) == 0, violations=violations)
