"""查询感知上下文选择器：基于关键词重叠、实体匹配与时效性加权，按 Token 预算筛选工具流水。

NOTE(M0.2): 原先还有一个 ``select_snapshots``（筛选领域结构化快照）。领域快照层
从未被写入过，2026-09 随整层删除，见 ``session_snapshot.py`` 的模块注释。
"""

from __future__ import annotations

import re

import structlog

from csagent.infra.config import PROJECT_ROOT
from csagent.memory.session_snapshot import ToolCallSnapshot
from csagent.memory.token_counter import TokenCounter
from csagent.prompts.domain_text import extract_domain_entities

logger = structlog.get_logger(__name__)

# 调试日志/仓库内部路径，注入下一轮只会带偏模型
_NOISE_TOOL_HINTS = (
    "llm_bodies",
    "data/audit",
    "data\\audit",
    ".git",
    ".venv",
    "Traceback",
    "命令失败",
)

# 仓库自身的落地路径片段：run_shell/grep 这类工具回显里出现它，说明模型在翻源码树
# 而非用户数据，注入下一轮没有价值。从 PROJECT_ROOT 推导，不写死任何部署路径。
_REPO_PATH_HINTS: tuple[str, ...] = tuple(
    part for part in (PROJECT_ROOT.name, PROJECT_ROOT.parent.name) if part
)
_REPO_BROWSING_TOOLS = frozenset({"grep", "glob", "run_shell", "list_dir"})


def _extract_tokens(text: str) -> set[str]:
    if not text:
        return set()
    tokens: set[str] = set(extract_domain_entities(text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    for word in re.findall(r"[A-Za-z0-9_]{2,}", text):
        tokens.add(word.lower())
    return tokens


class QueryAwareContextSelector:
    """根据当前 query 智能筛选和排序要注入的上下文，按 Token 预算精确裁剪。"""

    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self._counter = token_counter or TokenCounter()

    def select_tool_history(
        self,
        query: str,
        tool_calls: list[ToolCallSnapshot],
        token_budget: int = 150,
        max_items: int = 3,
    ) -> list[str]:
        """筛选并格式化最近成功的工具调用记录。"""
        if not tool_calls or token_budget <= 0:
            return []

        query_tokens = _extract_tokens(query)
        scored: list[tuple[float, ToolCallSnapshot]] = []

        for tc in tool_calls:
            summary = tc.result_summary or ""
            if not summary or summary.startswith(("错误", "失败", "Traceback", "Error")):
                continue
            blob = f"{tc.tool_name} {tc.arguments_summary} {summary}"
            if any(hint in blob for hint in _NOISE_TOOL_HINTS):
                continue
            if tc.tool_name in _REPO_BROWSING_TOOLS and any(
                hint in blob for hint in _REPO_PATH_HINTS
            ):
                continue
            text = blob
            overlap = len(query_tokens & _extract_tokens(text))
            score = float(overlap) * 2.0 + (tc.timestamp / 1e10)  # 近期时间略微加权
            scored.append((score, tc))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [tc for _score, tc in scored[:max_items]]
        # 恢复时间正序展示
        top.sort(key=lambda tc: tc.timestamp)

        lines: list[str] = []
        used_tokens = 0
        for tc in top:
            summary = self._counter.truncate_to_tokens(tc.result_summary, 40)
            line = f"- {tc.tool_name}: {summary}"
            cost = self._counter.count_text(line)
            if used_tokens + cost <= token_budget:
                lines.append(line)
                used_tokens += cost
            else:
                break
        return lines
