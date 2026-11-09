"""终止本轮：/api/chat/stop 取消正在跑的一轮；同会话并发请求被拒。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from csagent.api import router_chat
from csagent.infra import auth
from csagent.stream_events import AgentEventType, AgentStreamEvent

SECRET = "test-secret-that-is-long-enough-for-hmac-32"


class _SlowGateway:
    """先吐一段正文，然后一直挂着，模拟长任务。"""

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def handle(self, message):
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="部分回答")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="不该出现")


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch):
    settings = MagicMock()
    settings.agent_auth_enabled = True
    settings.agent_shared_secret = SECRET
    settings.agent_token_ttl_seconds = 1800
    settings.public_base_url = ""
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_chat.get_settings", lambda: settings)
    router_chat._active_turns.clear()

    app = FastAPI()
    app.include_router(router_chat.router)
    channel = MagicMock(is_running=True)
    app.state.channel_manager = MagicMock(get=MagicMock(return_value=channel))
    gateway = _SlowGateway()
    app.state.message_gateway = gateway
    agent = MagicMock()
    app.state.main_agent = agent
    return app, gateway, agent


def _hdr(uid: str) -> dict:
    return {"Authorization": "Bearer " + auth.issue_token(uid, ttl_seconds=600, secret=SECRET)}


def _events(raw: str) -> list[dict]:
    return [
        json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_stop_cancels_running_turn_and_rejects_concurrent(app_env):
    app, gateway, agent = app_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        collected: list[str] = []

        async def _stream() -> None:
            async with client.stream(
                "POST", "/api/chat/stream", json={"message": "长任务"}, headers=_hdr("u1")
            ) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    collected.append(chunk)

        stream_task = asyncio.create_task(_stream())
        # ASGITransport 会把响应体攒到结束才交付，所以这里看服务端登记而不是看流
        for _ in range(100):
            if router_chat._running_turn("web:u1") is not None:
                break
            await asyncio.sleep(0.02)
        assert router_chat._running_turn("web:u1") is not None
        await asyncio.sleep(0.05)

        # 同会话再发一轮：拒绝
        busy = await client.post("/api/chat/stream", json={"message": "再来"}, headers=_hdr("u1"))
        assert busy.status_code == 409
        # 别的会话不受影响地判定（这里只验证不是 409）
        assert router_chat._running_turn("web:u2") is None

        stop = await client.post("/api/chat/stop", headers=_hdr("u1"))
        assert stop.json() == {"stopped": True}
        agent.mark_stop_requested.assert_called_once_with("web:u1")

        await asyncio.wait_for(stream_task, timeout=5)
        await asyncio.wait_for(gateway.cancelled.wait(), timeout=5)
        types = [e["type"] for e in _events("".join(collected))]
        assert types[-1] == "stopped"
        assert "部分回答" in "".join(collected)
        assert "不该出现" not in "".join(collected)

        # 停止后会话空闲，可以再次停止（幂等）
        again = await client.post("/api/chat/stop", headers=_hdr("u1"))
        assert again.json() == {"stopped": False}


@pytest.mark.asyncio
async def test_stop_requires_auth(app_env):
    app, _, _ = app_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/api/chat/stop")
        assert resp.status_code == 401
