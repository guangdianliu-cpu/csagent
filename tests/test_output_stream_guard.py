"""输出护栏在流式路径上生效：片段出站前脱敏 / 拦截，全文不再重复追加。

以前的缺陷（已实测复现）：终答边生成边推给客户端，``OutputGuard.review()`` 等全文生成完
才跑，脱敏结果又作为一段 delta 追加——前端 ``appendDelta`` 与企微 ``answer +=`` 都是追加
语义，客户端最终看到「原文手机号 + 脱敏副本」；手机号规则还排在身份证前面，把身份证号
切成 ``1101011***4``，前 7 位露出。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from csagent.agent import BLOCKED_OUTPUT_NOTICE, BLOCKED_OUTPUT_REPLY, Agent
from csagent.common.secrets import SecretMasker
from csagent.guardrail.config_loader import load_guardrail_config
from csagent.guardrail.input_guard import GuardrailVerdict
from csagent.guardrail.output_guard import OutputGuard
from csagent.guardrail.rules import OutputRule
from csagent.memory.short_term import ShortTermMemory
from csagent.react.models import DomainTaskResult
from csagent.stream_events import AgentEventType

PHONE = "13812345678"
ID_CARD = "110101199003071234"
RAW = f"客户手机号 {PHONE}，身份证 {ID_CARD}，请核对。"


def _real_guard() -> OutputGuard:
    return OutputGuard.from_config(load_guardrail_config()["output_rules"])


def _feed_all(guard: OutputGuard, text: str, size: int) -> str:
    stream = guard.stream()
    out = [stream.feed(text[i:i + size]) for i in range(0, len(text), size)]
    out.append(stream.finish())
    return "".join(out)


# ---------------------------------------------------------------- 规则本身


def test_real_rules_mask_id_card_whole_and_phone() -> None:
    sanitized = _real_guard().mask(RAW)
    assert PHONE not in sanitized
    assert ID_CARD not in sanitized
    # 身份证不能被手机号规则先咬一口切坏（以前会剩下 1101011***4）
    assert "1101011" not in sanitized
    assert "1***" in sanitized and "****" in sanitized


def test_phone_rule_does_not_bite_into_longer_digit_runs() -> None:
    order_no = "2026101213812345678"  # 19 位：按银行卡口径整段遮，不能只遮中间的手机号
    sanitized = _real_guard().mask(f"订单号 {order_no}")
    assert "1***" not in sanitized
    assert order_no not in sanitized


# ---------------------------------------------------------------- 流式遮挡器


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
def test_stream_masking_matches_whole_text_masking(size: int) -> None:
    guard = _real_guard()
    assert _feed_all(guard, RAW, size) == guard.mask(RAW)


def test_stream_holds_unfinished_ascii_run_until_separator() -> None:
    stream = _real_guard().stream()
    assert stream.feed("电话138") == "电话"
    assert stream.feed("1234") == ""
    assert stream.feed("5678，好的") == "1***，好的"
    assert stream.finish() == ""


def test_stream_flushes_held_tail_on_finish() -> None:
    stream = _real_guard().stream()
    assert stream.feed("编号 ABC") == "编号 "
    assert stream.finish() == "ABC"


def test_stream_releases_very_long_ascii_runs() -> None:
    stream = _real_guard().stream()
    released = stream.feed("x" * 10000)
    assert 0 < len(released) < 10000
    assert len(released) + len(stream.finish()) == 10000


def test_stream_blocks_keyword_split_across_chunks() -> None:
    guard = OutputGuard([OutputRule(name="internal", action="block", keywords=["内部资料"])])
    stream = guard.stream()
    emitted = stream.feed("这是内部") + stream.feed("资料，别外传") + stream.finish()
    assert "内部" not in emitted
    assert stream.blocked
    assert stream.violations == ["internal: 命中拦截规则"]


def test_secret_values_rule_masks_known_secret_in_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-live-0123456789abcdef"
    monkeypatch.setattr("csagent.common.secrets._masker", SecretMasker([secret]))
    guard = _real_guard()
    text = f"配置里的 key 是 {secret} 。"
    for size in (1, 4, 9):
        out = _feed_all(guard, text, size)
        assert secret not in out
        assert "[已屏蔽]" in out


def test_secret_rule_is_skipped_when_no_secret_known() -> None:
    names = [r.name for r in _real_guard()._rules]
    assert "secret_values" not in names  # conftest 置空了遮挡器


# ---------------------------------------------------------------- Agent 端到端


class _PassInput:
    async def review(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(passed=True, violations=[])


class _Snapshots:
    def load(self, session_id: str) -> dict:
        return {}

    def update(self, *args, **kwargs) -> None:
        return None


def _agent(output_guard: OutputGuard, *, answer: str, reasoning: str = "", chunk: int = 6):
    async def fake_run(request, system_prompt, agent_name, on_thought=None,
                       on_progress=None, on_delta=None, on_reasoning=None):
        for i in range(0, len(reasoning), chunk):
            await on_reasoning(reasoning[i:i + chunk])
        for i in range(0, len(answer), chunk):
            await on_delta(answer[i:i + chunk])
        return DomainTaskResult(task_id="t", domain="generic", status="success",
                                answer_draft=answer)

    loop = MagicMock()
    loop.run = fake_run
    memory = ShortTermMemory()
    agent = Agent(
        react_loop=loop, tool_registry=MagicMock(), short_term_memory=memory,
        input_guard=_PassInput(), output_guard=output_guard,
        session_snapshot_store=_Snapshots(), show_thinking_process=False,
    )
    agent._write_audit = AsyncMock()  # type: ignore[method-assign]
    return agent, memory


async def _collect(agent: Agent) -> dict[AgentEventType, str]:
    seen: dict[AgentEventType, list[str]] = {}
    async for event in agent.handle_message_stream(session_id="s-stream", text="查一下"):
        seen.setdefault(event.event_type, []).append(event.content or "")
    return {k: "".join(v) for k, v in seen.items()}


@pytest.mark.asyncio
async def test_agent_never_streams_raw_pii_and_does_not_duplicate_answer() -> None:
    guard = _real_guard()
    agent, memory = _agent(guard, answer=RAW, reasoning=f"思考：客户电话 {PHONE}。")
    out = await _collect(agent)

    delta = out[AgentEventType.DELTA]
    assert PHONE not in delta and ID_CARD not in delta
    # 客户端拼起来恰好是一份脱敏后的答案，不再被追加第二遍
    assert delta == guard.mask(RAW)
    assert PHONE not in out.get(AgentEventType.REASONING, "")
    # 落库的同样是脱敏文本
    assert PHONE not in memory.load("s-stream")[-1].content
    kwargs = agent._write_audit.await_args.kwargs
    assert kwargs["status"] == "success"
    assert PHONE not in kwargs["final_reply"]


@pytest.mark.asyncio
async def test_agent_stops_stream_on_block_rule_and_records_blocked_status() -> None:
    guard = OutputGuard([OutputRule(name="internal", action="block", keywords=["内部资料"])])
    agent, memory = _agent(guard, answer="前文一切正常。后面是内部资料，绝密。")
    out = await _collect(agent)

    delta = out[AgentEventType.DELTA]
    assert "内部资料" not in delta and "绝密" not in delta
    assert delta.startswith("前文一切正常")
    assert delta.endswith(BLOCKED_OUTPUT_NOTICE)
    kwargs = agent._write_audit.await_args.kwargs
    assert kwargs["status"] == "blocked_output"
    assert "internal: 命中拦截规则" in kwargs["violations"]
    assert "内部资料" not in memory.load("s-stream")[-1].content


@pytest.mark.asyncio
async def test_agent_replaces_whole_reply_when_blocked_before_anything_streamed() -> None:
    guard = OutputGuard([OutputRule(name="internal", action="block", keywords=["内部资料"])])
    agent, _ = _agent(guard, answer="内部资料：……", chunk=64)
    out = await _collect(agent)
    assert out[AgentEventType.DELTA] == BLOCKED_OUTPUT_REPLY
