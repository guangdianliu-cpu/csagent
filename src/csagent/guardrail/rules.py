"""Guardrail 输入/输出规则模型（M2.1）。

配置驱动：规则本身不含业务逻辑分支，只负责「命中判定」；
是否拦截/告警/脱敏由 `InputGuard` / `OutputGuard` 按 `action` 字段执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuleResult:
    """单条规则的命中结果。"""

    triggered: bool
    description: str = ""


@dataclass
class InputRule:
    """输入侧规则：正则 / 关键词 / 最大长度。"""

    name: str
    enabled: bool = True
    action: str = "block"  # block | warn | log
    patterns: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    max_chars: int | None = None

    def __post_init__(self) -> None:
        self._compiled: list[re.Pattern[str]] = []
        for pattern in self.patterns:
            try:
                self._compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                # 配置里的坏正则不应让整个 Guardrail 崩溃，跳过该条模式
                continue

    def check(self, text: str) -> RuleResult:
        """按 max_chars → patterns → keywords 顺序判定是否命中。"""
        text = text or ""
        if self.max_chars is not None and len(text) > self.max_chars:
            return RuleResult(True, f"{self.name}: 输入超过 {self.max_chars} 字符")
        for pattern in self._compiled:
            if pattern.search(text):
                return RuleResult(True, f"{self.name}: 命中模式 {pattern.pattern}")
        lowered = text.lower()
        for kw in self.keywords:
            if kw and kw.lower() in lowered:
                return RuleResult(True, f"{self.name}: 命中关键词 {kw}")
        return RuleResult(False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputRule:
        return cls(
            name=str(data.get("name") or "unnamed"),
            enabled=bool(data.get("enabled", True)),
            action=str(data.get("action") or "block"),
            patterns=[str(p) for p in (data.get("patterns") or [])],
            keywords=[str(k) for k in (data.get("keywords") or [])],
            max_chars=data.get("max_chars"),
        )


@dataclass
class OutputRule:
    """输出侧规则：PII/敏感词正则或关键词，支持 mask / block / warn。"""

    name: str
    enabled: bool = True
    action: str = "warn"  # mask | block | warn
    pattern: str | None = None
    mask_with: str = "***"
    keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._compiled: re.Pattern[str] | None = None
        if self.pattern:
            try:
                self._compiled = re.compile(self.pattern)
            except re.error:
                self._compiled = None

    def matches(self, text: str) -> bool:
        text = text or ""
        if self._compiled is not None and self._compiled.search(text):
            return True
        if self.keywords:
            lowered = text.lower()
            return any(kw and kw.lower() in lowered for kw in self.keywords)
        return False

    def apply_mask(self, text: str) -> str:
        if self._compiled is None:
            return text
        return self._compiled.sub(self.mask_with, text or "")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputRule:
        return cls(
            name=str(data.get("name") or "unnamed"),
            enabled=bool(data.get("enabled", True)),
            action=str(data.get("action") or "warn"),
            pattern=data.get("pattern"),
            mask_with=str(data.get("mask_with") or "***"),
            keywords=[str(k) for k in (data.get("keywords") or [])],
        )
