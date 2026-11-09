"""多轮上下文：全轮全量工具。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.agent import Agent
from csagent.guardrail.input_guard import GuardrailVerdict
from csagent.memory.session_snapshot import SessionSnapshotStore
from csagent.memory.short_term import ShortTermMemory
from csagent.react.loop import ReactLoop

from tests.test_react_loop import StubToolRegistry, _plain_text_msg


class _FakeInputGuard:
    async def review(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(passed=True, violations=[])


class _FakeOutputGuard:
    def review(self, text: str):
        from csagent.guardrail.output_guard import OutputGuardVerdict

        return OutputGuardVerdict(passed=True, sanitized_text=text)


class CapturingLlm:
    def __init__(self) -> None:
        self.tool_names: list[list[str]] = []
        self.user_previews: list[str] = []

    async def chat(self, messages, **kwargs) -> str:
        return "ok"

    async def chat_stream(self, messages, **kwargs) -> str:
        return "ok"
        
    async def chat_with_tools(self, messages, tools, **kwargs):
        names = [((t.get("function") or {}).get("name") or "") for t in tools]
        self.tool_names.append(names)
        users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
        self.user_previews.append("\n".join(str(u) for u in users if isinstance(u, str)))
        return _plain_text_msg("请补充一下您具体想查什么。")


class WideStubRegistry(StubToolRegistry):
    def list_tool_schemas_for_agent(self, agent_name: str) -> list[dict]:
        names = [
            "read_file", "write_file", "list_dir", "edit_file",
            "grep", "glob", "run_shell", "run_script",
            "list_skills", "load_skill",
        ]
        return [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": n,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in names
        ]


@pytest.mark.asyncio
async def test_ambiguous_query_first_round_excludes_repo_search_tools():
    # 已改为首轮全量：含糊问题首轮同样发全量业务工具，不再裁剪；
    # 系统工具已下线，不应再出现。
    llm = CapturingLlm()
    loop = ReactLoop(
        llm_client=llm,
        tool_registry=WideStubRegistry(),
    )
    agent = Agent(
        react_loop=loop,
        tool_registry=MagicMock(),
        short_term_memory=ShortTermMemory(),
        input_guard=_FakeInputGuard(),
        output_guard=_FakeOutputGuard(),
        session_snapshot_store=SessionSnapshotStore(),
        show_thinking_process=False,
    )
    agent._tool_registry.list_tools_for_agent.return_value = []
    agent._write_audit = AsyncMock()

    async for _ in agent.handle_message_stream(
        session_id="s-route", text="墨迹阿婆"
    ):
        pass

    assert llm.tool_names, "应至少发起一轮带工具的 LLM 调用"
    first = set(llm.tool_names[0])
    # 首轮全量：业务工具都在
    assert "list_skills" in first
    assert "run_shell" in first
    assert "grep" in first
    assert "list_dir" in first
    # 系统工具已下线：不应再发给模型
    assert "__final_answer__" not in first
    assert "__escalate__" not in first
    assert "墨迹阿婆" in llm.user_previews[0]
    assert llm.user_previews[0].startswith("本轮用户问题")
    assert "动态上下文信息" not in llm.user_previews[0]
    assert "客户任务:" not in llm.user_previews[0]
