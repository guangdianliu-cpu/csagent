"""Prompt 布局优化器：稳定前缀对齐 + 3 层 Cache Breakpoint，最大化 Prompt Cache 命中率。

架构设计（借鉴 Claude Code & Codex CLI）：
[ Cache Breakpoint 1: 静态系统指令 + 确定性工具 Schemas ]  (跨请求复用)
                   ↓
[ Cache Breakpoint 2: 角色/项目持久记忆 (CLAUDE.md / AGENTS.md 模式) ] (会话级复用)
                   ↓
[ Cache Breakpoint 3: 压缩后的历史 Checkpoint 消息 ] (多轮对话稳定复用)
                   ↓
[ Dynamic Tail: 最近对话 + 相关快照 + 当前用户输入 ] (每轮动态变化)
"""

from __future__ import annotations

import json
from typing import Any

from csagent.memory.token_counter import TokenCounter
from csagent.react.models import DomainTaskRequest


class PromptLayoutOptimizer:
    """负责将 System Prompt、持久记忆、历史 Checkpoint、动态上下文组装为对齐缓存的消息序列。"""

    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self._counter = token_counter or TokenCounter()

    def sort_tool_schemas(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按工具名称对 tool schemas 进行确定性排序，防止字典乱序导致 Prompt Cache 失效。"""
        if not tools:
            return []
        return sorted(
            tools,
            key=lambda t: ((t.get("function") or {}).get("name", "") if isinstance(t, dict) else ""),
        )

    def initial_head_count(self, *, checkpoint_summary: str = "") -> int:
        """``assemble_initial_messages`` 产出的消息里，头部有几条是不可压缩的。

        循环内压缩（``HistoryCompactionPolicy.compact_loop_messages``）要靠它划保护区。
        改造前那边是用「content 里有没有『Checkpoint』这个词」猜的——本方法与
        ``assemble_initial_messages`` 贴在一起，布局一改这里必须跟着改，猜不出错位。

        构成：system(1) + Checkpoint 锚点 user/assistant(2，有摘要时) + 本轮问题 user(1)。
        """
        return 1 + (2 if checkpoint_summary.strip() else 0) + 1

    def assemble_initial_messages(
        self,
        system_prompt: str,
        request: DomainTaskRequest,
        *,
        persistent_instructions: str = "",
        checkpoint_summary: str = "",
        dynamic_context: str = "",
    ) -> list[dict[str, Any]]:
        """组装初始 ReAct 消息列表，严格分层以最大化 KV Cache 命中率。"""
        # 1. System Prompt（BP1: 核心系统指令 + BP2: 持久项目/角色规则）
        sys_blocks = [system_prompt.strip()]
        if persistent_instructions.strip():
            sys_blocks.append(f"## 持久规则与背景知识\n{persistent_instructions.strip()}")
        final_system_prompt = "\n\n".join(sys_blocks)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": final_system_prompt}
        ]

        # 2. 结构化历史 Checkpoint（BP3: 作为独立的早期 Assistant/User 摘要锚点）
        #
        # 必须明说它是「有损压缩产物、可能失真」，并给出与工具返回冲突时的优先级。
        # 否则模型会把 Checkpoint 当既定事实：曾有一次压缩把「某分类筛出 1773 条」
        # 记成「全量 1773 条」，下一轮模型看到工具返回 matched=1773 便认定筛选失效，
        # 连查三轮去证伪一个正确结果（见 audit 2026-08-16 14:47，14 轮里废掉 4 轮）。
        if checkpoint_summary.strip():
            messages.append({
                "role": "user",
                "content": (
                    "[早期对话历史 Checkpoint —— 由模型压缩生成，可能有遗漏或转述偏差]\n"
                    f"{checkpoint_summary.strip()}\n\n"
                    "使用规则：它只用于回忆此前做过什么、用户提过什么；"
                    "**任何与工具实时返回不一致的地方，一律以工具返回为准**，"
                    "不要为了迁就这份摘要去反复验证工具结果。"
                ),
            })
            messages.append({
                "role": "assistant",
                "content": (
                    "已了解上述早期对话概要。我会把它当作背景参考，"
                    "遇到与工具返回冲突时以工具返回为准。"
                ),
            })

        # 3. Dynamic Tail：本轮问题必须最先出现，避免被身份绑定/历史上下文带偏
        user_content_parts: list[str] = [f"本轮用户问题：\n{request.instruction.strip()}"]

        if dynamic_context.strip():
            user_content_parts.append(
                "此前对话与快照（仅供指代消解，不要当作本轮新问题）：\n"
                + dynamic_context.strip()
            )

        if request.memory_snapshot:
            snap_str = json.dumps(request.memory_snapshot, ensure_ascii=False)
            user_content_parts.append(f"编排上下文 memory_snapshot:\n{snap_str}")

        combined_user_text = "\n\n".join(user_content_parts)

        # 多模态支持
        if request.vision_parts:
            content: Any = [{"type": "text", "text": combined_user_text}, *request.vision_parts]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": combined_user_text})

        return messages
