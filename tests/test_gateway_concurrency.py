"""网关并发闸门（M2.2）测试。

主张两条：
1. 闸门是**过载保护**，不是排他锁——名额够的时候请求必须真的并行；
2. 满员时先给用户一句「已排队」再等，不能静默阻塞。静默等待的用户会以为消息没
   发出去而重发，那正好再多占一个名额，把拥塞推得更高。

此前 docs/架构图.md 与 docs/当前实现说明.md 都声称网关有并发限流，代码里并不存在。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import UnifiedMessage
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.user_progress import QUEUED_NOTICE


# ---------- 测试替身 ----------


def _agent_that_blocks_until(gate: asyncio.Event, *, inflight: list[int]):
    """造一个「开始后挂住直到放行」的 Agent，用来观测并发峰值。"""
    main_agent = MagicMock()

    async def _stream(**kwargs):
        inflight.append(len(inflight) + 1)
        await gate.wait()
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="done")

    main_agent.handle_message_stream = _stream
    return main_agent


def _msg(session_id: str = "s1") -> UnifiedMessage:
    return UnifiedMessage(channel="http_api", session_id=session_id, text="问题")


async def _drain(gateway: MessageGateway, message: UnifiedMessage) -> list[str]:
    return [e.content async for e in gateway.handle(message)]


# ---------- 名额充足：必须真并行 ----------


@pytest.mark.asyncio
async def test_requests_within_the_limit_run_in_parallel():
    """闸门不是排他锁：同一个人并发提两个问题照常并行。"""
    gate = asyncio.Event()
    started: list[int] = []
    gateway = MessageGateway(
        main_agent=_agent_that_blocks_until(gate, inflight=started),
        max_concurrent_requests=4,
    )

    tasks = [asyncio.create_task(_drain(gateway, _msg())) for _ in range(3)]
    await asyncio.sleep(0.05)  # 让三个都跑到阻塞点
    peak = len(started)
    gate.set()
    await asyncio.gather(*tasks)

    assert peak == 3, "串行版本这里恒为 1"


# ---------- 名额耗尽：先提示再排队 ----------


@pytest.mark.asyncio
async def test_overflow_request_is_told_it_is_queued_before_waiting():
    gate = asyncio.Event()
    gateway = MessageGateway(
        main_agent=_agent_that_blocks_until(gate, inflight=[]),
        max_concurrent_requests=1,
    )

    holder = asyncio.create_task(_drain(gateway, _msg("s1")))
    await asyncio.sleep(0.05)  # 让第一个占住唯一名额
    waiter = asyncio.create_task(_drain(gateway, _msg("s2")))
    await asyncio.sleep(0.05)

    assert not waiter.done(), "超额请求应该在等名额，而不是被直接拒绝"
    gate.set()
    first, second = await asyncio.gather(holder, waiter)

    assert first == ["done"], "占住名额的那个不该收到排队提示"
    assert second == [QUEUED_NOTICE, "done"], "排队提示必须在结果之前送达"


@pytest.mark.asyncio
async def test_slot_is_released_after_each_request():
    """名额不释放的话，第 N+1 个请求会永远卡住。"""
    gate = asyncio.Event()
    gate.set()  # 不阻塞，立刻跑完
    gateway = MessageGateway(
        main_agent=_agent_that_blocks_until(gate, inflight=[]),
        max_concurrent_requests=1,
    )

    for _ in range(3):
        assert await _drain(gateway, _msg()) == ["done"]


@pytest.mark.asyncio
async def test_abandoned_request_gives_its_slot_back():
    """客户端中途断开（生成器被关闭）也必须归还名额，否则闸门会被慢慢耗干。"""
    gate = asyncio.Event()
    gateway = MessageGateway(
        main_agent=_agent_that_blocks_until(gate, inflight=[]),
        max_concurrent_requests=1,
    )

    stream = gateway.handle(_msg())
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # 必须等取消真正落地，否则生成器还在运行中，aclose 会报错
    await stream.aclose()

    gate.set()
    assert await _drain(gateway, _msg()) == ["done"]


# ---------- 关闭限流 ----------


@pytest.mark.asyncio
async def test_zero_means_no_limiting():
    gate = asyncio.Event()
    started: list[int] = []
    gateway = MessageGateway(
        main_agent=_agent_that_blocks_until(gate, inflight=started),
        max_concurrent_requests=0,
    )

    tasks = [asyncio.create_task(_drain(gateway, _msg())) for _ in range(5)]
    await asyncio.sleep(0.05)
    peak = len(started)
    gate.set()
    await asyncio.gather(*tasks)

    assert peak == 5
