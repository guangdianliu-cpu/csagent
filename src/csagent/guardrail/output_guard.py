"""输出侧 Guardrail：配置驱动的规则引擎 + PII / 密钥脱敏（M2.1）。

`OutputGuard()`（无规则）保持向后兼容：原样放行，用于未接入配置的场景/单测骨架。
生产环境请用 `OutputGuard.from_config(...)` 从 `configs/guardrail.yaml` 构建。

**流式输出必须在出站前过护栏**：终答是边生成边推给客户端的，等全文生成完再
``review()`` 已经晚了——前端与企微都是追加语义，脱敏结果只会作为第二段追加上去，
原文早已发出（以前就是这样：手机号原样推给了客户端，后面再跟一份脱敏副本）。
所以 Agent 对每个流式通道开一个 :class:`StreamingMasker`，片段先过它再发。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from csagent.common.secrets import DEFAULT_SECRET_MASK, get_secret_masker, is_token_char
from csagent.guardrail.rules import OutputRule

logger = structlog.get_logger(__name__)

# 流式遮挡：尾部 ASCII 串超过这个长度（base64、超长 URL）就不再整串等，
# 只扣住最后 _HOLD_KEEP 个字符防止切开常见号码 / 密钥，避免无限缓冲
_HOLD_LIMIT = 4096
_HOLD_KEEP = 256


class OutputGuardVerdict(BaseModel):
    """输出侧检测结果。"""

    passed: bool
    violations: list[str] = Field(default_factory=list)
    sanitized_text: str | None = None


class OutputGuard:
    """输出审查入口；无规则时原样返回正文（向后兼容对内放行场景）。"""

    def __init__(self, rules: list[OutputRule] | None = None, *, stream_holdback: int = 0) -> None:
        self._rules = rules or []
        block_keyword_lengths = [
            len(kw)
            for rule in self._rules
            if rule.enabled and rule.action == "block"
            for kw in rule.keywords
            if kw
        ]
        # 关键词拦截要在关键词的第一个字发出之前就判定，得扣住「最长关键词 - 1」个字符
        self._stream_holdback = max(
            [stream_holdback, *(n - 1 for n in block_keyword_lengths)], default=0
        )

    @classmethod
    def from_config(cls, rules_config: list[dict] | None) -> OutputGuard:
        """从 `guardrail.yaml` 的 `output_rules` 列表构建。

        ``source: env_secrets`` 的规则不写 pattern：启动时从 .env / 进程环境收集
        名字像密钥的变量值生成（见 common/secrets.py）；一个都没有时跳过该规则。
        """
        rules: list[OutputRule] = []
        holdback = 0
        for item in rules_config or []:
            if str(item.get("source") or "") == "env_secrets":
                masker = get_secret_masker()
                if not masker.count:
                    continue
                rules.append(
                    OutputRule(
                        name=str(item.get("name") or "secret_values"),
                        enabled=bool(item.get("enabled", True)),
                        action="mask",
                        pattern=masker.pattern_source,
                        mask_with=str(item.get("mask_with") or DEFAULT_SECRET_MASK),
                    )
                )
                holdback = max(holdback, masker.stream_holdback)
                continue
            rules.append(OutputRule.from_dict(item))
        return cls(rules, stream_holdback=holdback)

    @property
    def stream_holdback(self) -> int:
        return self._stream_holdback

    def mask(self, text: str) -> str:
        """只做脱敏（mask 规则按配置顺序依次应用），不判拦截。"""
        sanitized = text or ""
        for rule in self._rules:
            if rule.enabled and rule.action == "mask":
                sanitized = rule.apply_mask(sanitized)
        return sanitized

    def block_hits(self, text: str) -> list[str]:
        """命中的拦截规则（描述列表）；空列表表示放行。"""
        return [
            f"{rule.name}: 命中拦截规则"
            for rule in self._rules
            if rule.enabled and rule.action == "block" and rule.matches(text or "")
        ]

    def stream(self) -> StreamingMasker:
        """为一路流式输出开一个遮挡器（每轮、每个通道各一个，不能复用）。"""
        return StreamingMasker(self)

    def review(self, text: str) -> OutputGuardVerdict:
        sanitized = self.mask(text)
        violations = self.block_hits(sanitized)
        for rule in self._rules:
            if rule.enabled and rule.action == "warn" and rule.matches(sanitized):
                logger.warning("output_guard_warn", rule=rule.name)
        return OutputGuardVerdict(
            passed=len(violations) == 0,
            violations=violations,
            sanitized_text=sanitized or None,
        )


class StreamingMasker:
    """流式片段的增量脱敏与拦截。

    片段先进缓冲，只放出「之后不可能再变」的前缀：

    - 尾部连续的 ASCII 可见字符先扣住——号码、证件号、密钥都由这类字符组成，
      可能还没出完；遇到空格、中文或标点（串结束）才连同它一起放出；
    - 有关键词拦截规则或含非 ASCII 字符的密钥时，再多扣「最长者长度 - 1」个字符；
    - 永远不在 ASCII 串中间切，保证正则的数字边界判断与整段处理一致。

    放出前对待发文本整段做拦截判定，命中就不再放出任何内容（``blocked`` 置真），
    由调用方补一句说明；放出的部分逐段脱敏。不跨片段成立的正则（例如中间带空格的号码）
    不在这里保证，全文的 ``review()`` 仍会兜一遍。
    """

    def __init__(self, guard: OutputGuard) -> None:
        self._guard = guard
        self._pending = ""
        self._blocked = False
        self._violations: list[str] = []

    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def violations(self) -> list[str]:
        return list(self._violations)

    def feed(self, chunk: str) -> str:
        """喂入一个片段，返回现在可以安全发出的（已脱敏）文本，可能为空串。"""
        if self._blocked or not chunk:
            return ""
        self._pending += chunk
        return self._release(self._safe_cut(self._pending))

    def finish(self) -> str:
        """流结束：放出剩余缓冲（已脱敏）。拦截后恒为空串。"""
        if self._blocked:
            self._pending = ""
            return ""
        return self._release(len(self._pending))

    def _release(self, cut: int) -> str:
        if cut <= 0:
            return ""
        hits = self._guard.block_hits(self._guard.mask(self._pending))
        if hits:
            self._blocked = True
            self._violations.extend(hits)
            self._pending = ""
            logger.warning("output_guard_stream_blocked", violations=hits)
            return ""
        head, self._pending = self._pending[:cut], self._pending[cut:]
        return self._guard.mask(head)

    def _safe_cut(self, text: str) -> int:
        """可以放出的前缀长度。"""
        size = len(text)
        run_start = size
        while run_start > 0 and is_token_char(text[run_start - 1]):
            run_start -= 1
        if size - run_start > _HOLD_LIMIT:
            return size - _HOLD_KEEP
        cut = run_start
        holdback = self._guard.stream_holdback
        if holdback:
            cut = min(cut, size - holdback)
            # 退到 ASCII 串的边界，不把串切开
            while 0 < cut < size and is_token_char(text[cut - 1]) and is_token_char(text[cut]):
                cut -= 1
        return max(cut, 0)
