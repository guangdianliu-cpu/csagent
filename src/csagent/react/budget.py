"""本轮预算感知：把轮数、工具次数、时长的余量告诉模型，让它自己安排收口。

以前三项上限只在代码里生效：模型不知道还剩多少，做到一半被软截止 / 轮数 / 次数
上限打断，只能由 force_finalize 仓促收口。现在分两步告诉它：

1. 开局：系统提示词末尾附一段固定的预算说明（数值来自配置、每轮相同，不影响前缀缓存）；
2. 循环中：任一项用到 :data:`WARN_RATIO` 时提醒一次「优先完成关键部分」，
   用到 :data:`FINAL_RATIO` 或只剩最后几轮时提醒一次「停止调用工具、直接作答」。
   提醒附在本轮最后一条工具结果后面——不插新的 user 消息，避免打乱
   assistant(tool_calls) → tool 的消息顺序（部分服务商对此校验严格）。

这里只负责「告诉模型」；到点的硬约束仍由 loop 的软截止、StallGuard、max_rounds 执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

# 任一项用到这个比例：提醒收敛
WARN_RATIO = 0.7
# 任一项用到这个比例：要求停止调用工具、直接作答
FINAL_RATIO = 0.9
# 轮数余量不超过这个数时也按「最后提醒」处理（比例对小上限不灵敏）
FINAL_ROUNDS_LEFT = 2

NOTICE_PREFIX = "[系统提示·本轮预算]"


class BudgetLevel(IntEnum):
    OK = 0
    WARN = 1
    FINAL = 2


def _minutes(seconds: float) -> str:
    """秒数 → 给模型看的分钟数（不足 1 分钟按 1 分钟说，避免「剩 0 分钟」）。"""
    return f"{max(1, round(seconds / 60))} 分钟"


@dataclass
class TurnBudget:
    """一次 ReAct 运行的预算与已发出的提醒级别。

    :param max_rounds: 最大轮数
    :param max_tool_calls: 最大工具调用次数；0 表示不限
    :param time_limit_seconds: 软截止时长；None / 0 表示不限
    :param started_at: 开始时刻（与 ``now`` 同一时钟）
    """

    max_rounds: int
    max_tool_calls: int = 0
    time_limit_seconds: float | None = None
    started_at: float = 0.0
    _sent: BudgetLevel = field(default=BudgetLevel.OK, init=False)

    @property
    def sent_level(self) -> BudgetLevel:
        """已经发出过的最高提醒级别。"""
        return self._sent

    def system_prompt_section(self) -> str:
        """开局附在系统提示词末尾的预算说明。"""
        limits = [f"最多 {self.max_rounds} 轮推理"]
        if self.max_tool_calls:
            limits.append(f"{self.max_tool_calls} 次工具调用")
        if self.time_limit_seconds:
            limits.append(f"约 {_minutes(self.time_limit_seconds)}")
        return (
            "## 本轮预算\n"
            f"回答这个问题你有{'、'.join(limits)}。超出后系统会停止工具调用、"
            "让你基于已有结果直接作答。请据此安排：先做最关键的步骤，"
            "能并行的工具调用放在同一轮；接近上限时系统会提醒你。"
        )

    def _ratios(self, round_no: int, tool_calls: int, now: float) -> list[float]:
        ratios = [round_no / self.max_rounds] if self.max_rounds else []
        if self.max_tool_calls:
            ratios.append(tool_calls / self.max_tool_calls)
        if self.time_limit_seconds:
            ratios.append((now - self.started_at) / self.time_limit_seconds)
        return ratios

    def level(self, round_no: int, tool_calls: int, now: float) -> BudgetLevel:
        """``round_no`` 轮结束时的预算级别。"""
        used = max(self._ratios(round_no, tool_calls, now), default=0.0)
        if used >= FINAL_RATIO or (
            self.max_rounds and self.max_rounds - round_no <= FINAL_ROUNDS_LEFT
        ):
            return BudgetLevel.FINAL
        if used >= WARN_RATIO:
            return BudgetLevel.WARN
        return BudgetLevel.OK

    def status_line(self, round_no: int, tool_calls: int, now: float) -> str:
        parts = [f"已用 {round_no}/{self.max_rounds} 轮"]
        if self.max_tool_calls:
            parts.append(f"工具调用 {tool_calls}/{self.max_tool_calls} 次")
        if self.time_limit_seconds:
            left = max(0.0, self.time_limit_seconds - (now - self.started_at))
            parts.append(f"剩余约 {_minutes(left)}")
        return "，".join(parts)

    def notice(self, round_no: int, tool_calls: int, now: float) -> str | None:
        """本轮结束时要给模型的提醒；每个级别只发一次，没有新级别时返回 None。"""
        level = self.level(round_no, tool_calls, now)
        if level <= self._sent:
            return None
        self._sent = level
        status = self.status_line(round_no, tool_calls, now)
        if level == BudgetLevel.FINAL:
            return (
                f"{NOTICE_PREFIX} {status}，预算即将用尽。请不要再调用工具，"
                "下一步直接基于已有结果给出完整答复；没做完的部分如实说明还缺什么、"
                "用户可以怎样继续。"
            )
        return (
            f"{NOTICE_PREFIX} {status}，已用掉大部分预算。请收敛：只做完成任务"
            "必需的步骤，不再展开新的探索方向，尽快进入作答。"
        )
