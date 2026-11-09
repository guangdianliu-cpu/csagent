"""react.loop.ReactLoop 的核心可靠性行为测试（Function Calling 版）。

使用桩 LLM / 工具，不依赖真实网络。桩 LLM 返回 OpenAI 格式的 assistant message dict。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from csagent.react.loop import ReactLoop
from csagent.react.models import CustomerScope, DomainTaskRequest
from csagent.react.narration import looks_like_interim_narration
from csagent.tools.registry import ToolOutcome


# ---------------------------------------------------------------------------
# 辅助：构造 assistant message
# ---------------------------------------------------------------------------

def _tool_call_msg(
    tool_name: str,
    arguments: dict,
    call_id: str = "call_1",
    content: str | None = None,
) -> dict[str, Any]:
    """构造带 tool_calls 的 assistant message。"""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _final_answer_msg(
    answer: str,
    confidence: float = 0.9,
    content: str | None = None,
) -> dict[str, Any]:
    """兼容别名：系统工具已下线，收口一律走纯文本。confidence/content 参数忽略。"""
    return _plain_text_msg(answer)


def _escalate_msg(reason: str, content: str | None = None) -> dict[str, Any]:
    """兼容别名：转人工即纯文本说明，不再有独立状态。"""
    return _plain_text_msg(reason)


def _plain_text_msg(text: str) -> dict[str, Any]:
    """构造不含 tool_calls 的纯文本 assistant message。"""
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": None,
    }


# ---------------------------------------------------------------------------
# 桩 LLM 客户端
# ---------------------------------------------------------------------------

class ScriptedLlmClient:
    """按顺序返回预设 assistant message 的桩 LLM 客户端。"""

    def __init__(self, replies: list[dict[str, Any] | None]) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def chat(self, messages, **kwargs) -> str | None:
        """非 tool-calling 场景（如 force_finalize）。"""
        self.calls += 1
        if not self._replies:
            return None
        msg = self._replies.pop(0)
        if msg is None:
            return None
        return msg.get("content") or ""

    async def chat_stream(self, messages, **kwargs) -> str | None:
        on_reasoning = kwargs.get("on_reasoning")
        content = await self.chat(messages)
        if content is not None and on_reasoning is not None:
            await on_reasoning("(mock reasoning)")
        return content

    async def chat_with_tools(self, messages, tools, **kwargs) -> dict[str, Any] | None:
        """Function calling 场景。"""
        self.calls += 1
        if not self._replies:
            return None
        return self._replies.pop(0)


class SlowLlmClient:
    """每次调用都会挂起，用于验证整体超时保护。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def chat(self, messages, **kwargs) -> str | None:
        await asyncio.sleep(self._delay)
        return "太慢了"

    async def chat_stream(self, messages, **kwargs) -> str | None:
        return await self.chat(messages)

    async def chat_with_tools(self, messages, tools, **kwargs) -> dict[str, Any] | None:
        await asyncio.sleep(self._delay)
        return _final_answer_msg("太慢了")


# ---------------------------------------------------------------------------
# 桩 ToolRegistry
# ---------------------------------------------------------------------------

class StubToolRegistry:
    """记录调用参数并返回固定字符串的桩工具注册中心。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute_detailed(self, tool_name, arguments, scope, agent_name):
        self.calls.append((tool_name, arguments))
        return ToolOutcome(f"工具{tool_name}返回: mock-data")

    def list_tool_schemas_for_agent(self, agent_name: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "trade_query",
                    "description": "查询交易数据",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                    },
                },
            }
        ]


# ---------------------------------------------------------------------------
# 请求工厂
# ---------------------------------------------------------------------------

def _make_request(max_rounds: int = 8) -> DomainTaskRequest:
    return DomainTaskRequest(
        task_id="t1",
        domain="trade",
        instruction="查询持仓",
        customer_scope=CustomerScope(session_id="s1"),
        max_rounds=max_rounds,
        trace_id="trace1",
    )


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_call_tool_then_final_answer():
    llm = ScriptedLlmClient([
        _tool_call_msg("trade_query", {"entity": "available_shares"}, call_id="call_1", content="查一下"),
        _final_answer_msg("您持有XX基金", confidence=0.9),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="trade_agent")

    assert result.status == "success"
    assert result.answer_draft == "您持有XX基金"
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_plain_text_fallback_when_no_tool_calls():
    """模型未调用任何工具、直接输出合格终答 → 兜底为 final_answer。"""
    plain_text = "您好！这是一个简单问题的直接回答，无需查询任何工具。"
    llm = ScriptedLlmClient([
        _plain_text_msg(plain_text),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="trade_agent")

    assert result.status == "success"
    assert result.answer_draft == plain_text
    assert llm.calls == 1
    assert len(tools.calls) == 0


@pytest.mark.asyncio
async def test_interim_plain_text_is_rejected_then_final_answer():
    """「我来查一下」等中间旁白不能直接收口，需重试后才接受终答。"""
    llm = ScriptedLlmClient([
        _plain_text_msg("我来查一下持仓"),
        _final_answer_msg("您当前持仓为 XX 基金 100 份。", confidence=0.9),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_invalid_replies=3)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "持仓" in (result.answer_draft or "")
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_reading_narration_is_rejected_without_tool_call():
    """「正在读取 xxx.md」不能当作终答直接结束。"""
    llm = ScriptedLlmClient([
        _plain_text_msg("（正在读取 methodology-固收-plus.md 文档…）"),
        _final_answer_msg("固收+三级分类包括积极型、平衡型等。", confidence=0.9),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_invalid_replies=3)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "积极型" in (result.answer_draft or "")
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_load_skill_narration_is_rejected():
    """「我先加载…Skill…」不能当终答（audit b1d42d05）。"""
    llm = ScriptedLlmClient([
        _plain_text_msg(
            "我先加载基金研究数据库的Skill，查看是否有持仓消费红利股相关的字段或标签。"
        ),
        _final_answer_msg(
            "消费服务主题里红利因子靠前的有：鹏华策略优选（160627.OF）等。",
            confidence=0.9,
        ),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools, max_invalid_replies=3)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "鹏华策略优选" in (result.answer_draft or "")
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_plain_text_narration_rejected_then_answered():
    """纯文本旁白被拒后，重试给出终答（替代已下线的工具收口拒绝用例）。"""
    llm = ScriptedLlmClient([
        _plain_text_msg("我先加载 Skill，查看是否有相关字段。"),
        _plain_text_msg("按红利因子排序，消费服务主题前列为 A、B。"),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "红利因子" in (result.answer_draft or "")
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_force_finalize_stream_rejects_interim_then_retries():
    """强制收口流式：首轮旁白不推送，重试后推送真终答。"""
    llm = ScriptedLlmClient([
        # 打满轮次：一直调工具直到 max_rounds，然后 force_finalize 走 chat_stream
        _tool_call_msg("trade_query", {}, call_id="c1"),
        _plain_text_msg("我先加载Skill，查看是否有相关字段。"),
        _plain_text_msg("消费服务主题按红利因子靠前：基金甲、基金乙。"),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)
    deltas: list[str] = []

    async def on_delta(chunk: str) -> None:
        deltas.append(chunk)

    result = await loop.run(
        _make_request(max_rounds=1),
        system_prompt="sys",
        agent_name="a",
        on_delta=on_delta,
    )

    assert result.status == "success"
    assert "基金甲" in (result.answer_draft or "")
    assert deltas == ["消费服务主题按红利因子靠前：基金甲、基金乙。"]
    assert all("我先加载" not in d for d in deltas)


def test_looks_like_interim_narration_markers():
    assert looks_like_interim_narration(
        "我先加载基金研究数据库的Skill，查看是否有持仓消费红利股相关的字段或标签。"
    )
    assert looks_like_interim_narration("正在加载 skill 说明…")
    assert not looks_like_interim_narration(
        "消费服务主题里红利因子靠前的有鹏华策略优选（160627.OF），近1年收益-10%。"
    )


def test_narration_markers_ignored_deep_in_body():
    """audit 20260904_174358_181：正文/表格里的 load_skill 与「查看」不算旁白。

    复刻事故形态：完整终答带「本轮工具调用」表格（含 load_skill）+
    口径说明（含「查看」），全文扫描曾把它误杀为旁白。
    """
    body = (
        "按内部研究资料库目录，最新20条报告如下。共1518条记录。\n\n"
        "| # | 日期 | 报告标题 |\n|---|---|---|\n"
        + "".join(f"| {i} | 2026-09-0{i} | 报告标题{i} |\n" for i in range(1, 10))
        + "\n口径说明：\n- 部分附件解析状态为 failed。\n"
        "- 如需查看某份报告的具体内容，请提供完整文件名。\n\n"
        "---\n\n**本轮工具调用**\n\n| 工具 | 返回内容 |\n|---|---|\n"
        "| load_skill(internal-research) | 返回检索操作说明 |\n"
        "| run_script(ask.py) | 返回报告目录共1518条 |"
    )
    assert len(body.replace(" ", "")) > 300
    assert not looks_like_interim_narration(body)
    assert not looks_like_interim_narration(body, for_final_answer=True)


def test_narration_at_head_still_caught():
    """开头窗口内的引导旁白仍然拦截。"""
    assert looks_like_interim_narration("我先加载Skill，查看是否有相关字段。")
    assert looks_like_interim_narration(
        "我先加载Skill，查看是否有相关字段。", for_final_answer=True
    )
    assert looks_like_interim_narration("好的，让我查一下最近的报告目录。")


def test_has_answer_substance_signals():
    from csagent.react.narration import has_answer_substance

    assert has_answer_substance("字" * 300)  # 长文
    assert has_answer_substance("| a | b |\n|---|---|\n| 1 | 2 |")  # markdown 表格
    assert has_answer_substance("最新报告共20条，均已完成索引。")  # 含数字
    assert not has_answer_substance("我来查一下持仓")  # 纯旁白
    assert not has_answer_substance("")
    assert not has_answer_substance(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plain_text_fail_open_when_substantive():
    """旁白特征命中但含实质内容（数字/表格）→ 直接放行，不重试。"""
    substantive = (
        "我先加载了Skill并查了报告目录，最新20条如下：\n\n"
        "| # | 日期 | 报告标题 |\n|---|---|---|\n"
        "| 1 | 2026-09-04 | 9月月报 |\n| 2 | 2026-09-03 | 每日观察 |"
    )
    llm = ScriptedLlmClient([_plain_text_msg(substantive)])
    loop = ReactLoop(llm_client=llm, tool_registry=StubToolRegistry())

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "9月月报" in (result.answer_draft or "")
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_invalid_tool_arguments_json_returns_tool_error_then_continues():
    """arguments 非法 JSON 时回写 tool error，不静默空参执行。"""
    bad_args_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_bad",
                "type": "function",
                "function": {
                    "name": "trade_query",
                    "arguments": "{not-json",
                },
            }
        ],
    }
    llm = ScriptedLlmClient([
        bad_args_msg,
        _final_answer_msg("已根据说明完成回复", confidence=0.9),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert tools.calls == []
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_escalate_is_plain_text_now():
    """转人工已无独立工具/状态：模型直接输出说明文本即收口。"""
    llm = ScriptedLlmClient([
        _escalate_msg("用户需求超出我的能力范围"),
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "超出" in result.answer_draft


@pytest.mark.asyncio
async def test_repeated_identical_tool_call_forces_finalize():
    call_msg = _tool_call_msg(
        "trade_query",
        {
            "entity": "available_shares",
            "filters": [{"field": "fund_code", "op": "eq", "value": "same"}],
        },
    )
    llm = ScriptedLlmClient([
        call_msg,
        call_msg,
        call_msg,
        # force_finalize 会调 chat() 拿纯文本
        {"role": "assistant", "content": "收口了", "tool_calls": None},
    ])
    tools = StubToolRegistry()
    loop = ReactLoop(
        llm_client=llm,
        tool_registry=tools,
        stall_repeat_threshold=3,
    )

    captured: list[list[dict]] = []
    original_chat = llm.chat

    async def capturing_chat(messages, **kwargs):
        captured.append(list(messages))
        return await original_chat(messages, **kwargs)

    llm.chat = capturing_chat  # type: ignore[method-assign]

    result = await loop.run(_make_request(max_rounds=10), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    # 停滞守卫在检测到第 3 次重复调用意图时即强制收口，实际只执行了 2 次工具调用
    assert len(tools.calls) == 2
    assert captured, "force_finalize 应调用 chat"
    finalize_messages = captured[0]
    last_assistant = next(
        m
        for m in reversed(finalize_messages)
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    pending_ids = {tc["id"] for tc in last_assistant["tool_calls"]}
    tool_ids = {
        m.get("tool_call_id")
        for m in finalize_messages
        if m.get("role") == "tool"
    }
    assert pending_ids.issubset(tool_ids)


@pytest.mark.asyncio
async def test_overall_hard_timeout_returns_timeout_status(monkeypatch):
    """收口本身也卡住时，硬超时（软超时 + 宽限）仍会兜底返回 timeout。"""
    import csagent.react.loop as loop_mod

    monkeypatch.setattr(loop_mod, "HARD_TIMEOUT_GRACE_SECONDS", 0.0)
    llm = SlowLlmClient(delay=0.3)
    tools = StubToolRegistry()
    loop = ReactLoop(
        llm_client=llm,
        tool_registry=tools,
        overall_timeout_seconds=0.05,
    )

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "timeout"
    assert result.confidence == 0.0


class _SlowToolLoopLlm:
    """每轮都慢慢地发起一次新工具调用；无 tools 的收口调用返回固定终答。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.rounds = 0
        self.finalize_messages: list[list[dict[str, Any]]] = []

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.rounds += 1
        await asyncio.sleep(self._delay)
        return _tool_call_msg("query_trade", {"n": self.rounds}, call_id=f"c{self.rounds}")

    async def chat(self, messages, **kwargs):
        self.finalize_messages.append(list(messages))
        return "根据已查到的结果：共 3 条记录。"

    async def chat_stream(self, messages, **kwargs):
        return await self.chat(messages)


@pytest.mark.asyncio
async def test_soft_deadline_finalizes_with_gathered_results():
    """到软截止不再开新一轮，而是基于已有工具结果收口，不丢弃已做的工作。"""
    llm = _SlowToolLoopLlm(delay=0.03)
    tools = StubToolRegistry()
    loop = ReactLoop(
        llm_client=llm,
        tool_registry=tools,
        overall_timeout_seconds=0.1,
        max_tool_calls=100,
    )

    result = await loop.run(_make_request(max_rounds=50), system_prompt="sys", agent_name="a")

    assert result.status == "success"
    assert "共 3 条记录" in result.answer_draft
    assert tools.calls, "截止前的工具调用应已执行"
    assert llm.rounds < 50
    # 收口时带上了已经拿到的工具结果
    assert any(m.get("role") == "tool" for m in llm.finalize_messages[0])


@pytest.mark.asyncio
async def test_llm_unavailable_forces_finalize_failure():
    llm = ScriptedLlmClient([None])
    tools = StubToolRegistry()
    loop = ReactLoop(llm_client=llm, tool_registry=tools)

    result = await loop.run(_make_request(), system_prompt="sys", agent_name="a")

    assert result.status == "failed"


def test_truncate_tool_result_keeps_short_json():
    loop = ReactLoop(llm_client=object(), tool_registry=object())
    raw = json.dumps({"ok": True, "count": 3}, ensure_ascii=False)
    assert loop._truncate_tool_result(raw) == raw


def test_truncate_tool_result_summarizes_large_json_object():
    """外置失败时的硬截断回落：字段很多的 JSON 应压成预览，而不是从中间切断。"""
    loop = ReactLoop(llm_client=object(), tool_registry=object(), max_tool_result_chars=500)
    payload = {f"field_{i}": i for i in range(80)}
    raw = json.dumps(payload, ensure_ascii=False)
    assert len(raw) > 500
    out = loop._truncate_tool_result(raw)
    assert "_summary" in out
    assert "展示已截断" in out


def test_truncate_tool_result_plain_text_uses_char_limit():
    loop = ReactLoop(llm_client=object(), tool_registry=object(), max_tool_result_chars=500)
    raw = "x" * 800
    out = loop._truncate_tool_result(raw)
    assert len(out) < len(raw)
    assert "展示已截断" in out
