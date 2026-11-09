"""网页确认卡片：SSE 下发 challenge，点击后按保存的参数执行，再带着结果续跑原任务。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from csagent.agent import ApprovedAction
from csagent.api import router_chat
from csagent.infra import auth
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.tools.approvals import ApprovalStore

SECRET = "test-secret-that-is-long-enough-for-hmac-32"


class _ChallengeGateway:
    """吐一条待确认事件和一句正文，然后结束。收到的消息记在 ``messages`` 里。"""

    def __init__(self) -> None:
        self.messages = []

    async def handle(self, message):
        self.messages.append(message)
        yield AgentStreamEvent(
            event_type=AgentEventType.CHALLENGE,
            content=json.dumps(
                {
                    "task_id": "task_abc",
                    "tool": "send_file",
                    "summary": "把报表发到群里",
                    "reason": "群里可见",
                    "code": "1234",
                },
                ensure_ascii=False,
            ),
        )
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="等你确认")


def _hdr(uid: str) -> dict:
    return {"Authorization": "Bearer " + auth.issue_token(uid, ttl_seconds=600, secret=SECRET)}


def _events(raw: str) -> list[dict]:
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


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

    store = ApprovalStore()
    agent = MagicMock()
    agent.approvals = store

    calls: list[str] = []

    async def _execute(pending):
        calls.append(pending.task_id)
        note = f"[系统] 已执行：{pending.summary}"
        store.complete(
            pending.task_id, result_text="ok", result_status="success", continuation=note
        )
        return ApprovedAction(
            task_id=pending.task_id, status="success", text=f"已执行：{pending.summary}",
            continuation=note,
        )

    agent.execute_approved_action = _execute
    agent.challenge_calls = calls

    app = FastAPI()
    app.include_router(router_chat.router)
    channel = MagicMock(is_running=True)
    app.state.channel_manager = MagicMock(get=MagicMock(return_value=channel))
    app.state.message_gateway = _ChallengeGateway()
    app.state.main_agent = agent
    return app, store, agent


@pytest.mark.asyncio
async def test_stream_forwards_challenge_without_the_code(app_env):
    app, _, _ = app_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/api/chat/stream", json={"message": "发到群里"}, headers=_hdr("u1")
        )
    assert resp.status_code == 200
    events = _events(resp.text)
    challenge = next(item for item in events if item["type"] == "challenge")
    assert challenge["task_id"] == "task_abc"
    assert challenge["tool"] == "send_file"
    assert challenge["reason"] == "群里可见"
    assert "code" not in challenge
    assert "1234" not in json.dumps(challenge, ensure_ascii=False)


@pytest.mark.asyncio
async def test_confirm_runs_saved_action_and_cancel_drops_it(app_env):
    app, store, agent = app_env
    pending = store.request(
        "web:u1",
        "send_file",
        {"path": "a.xlsx"},
        summary="把报表发到群里",
        rule_id="group-send",
        user_id="u1",
        reason="群里可见",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        listed = await client.get("/api/chat/challenges", headers=_hdr("u1"))
        assert listed.status_code == 200
        assert listed.json()["items"][0]["task_id"] == pending.task_id

        # 别人的会话看不见，也不会把它点掉
        foreign = await client.post(
            "/api/chat/challenge",
            json={"task_id": pending.task_id, "action": "confirm"},
            headers=_hdr("u2"),
        )
        assert foreign.status_code == 200
        assert foreign.json()["status"] == "missing"
        assert store.lookup(pending.task_id) is not None

        decided = await client.post(
            "/api/chat/challenge",
            json={"task_id": pending.task_id, "action": "confirm"},
            headers=_hdr("u1"),
        )
    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == "approved"
    assert body["reply"] == "已执行：把报表发到群里"
    assert body["resume_task_id"] == pending.task_id
    # 执行完保留到过期（续跑与防重放要用），但不能再点
    assert store.lookup(pending.task_id).status == "done"
    assert store.list_pending("web:u1") == []

    other = store.request(
        "web:u1",
        "http_call",
        {"url": "https://example.test"},
        summary="外呼",
        rule_id="http-call",
        user_id="u1",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        cancelled = await client.post(
            "/api/chat/challenge",
            json={"task_id": other.task_id, "action": "cancel"},
            headers=_hdr("u1"),
        )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["resume_task_id"] == other.task_id
    assert store.lookup(other.task_id).status == "cancelled"
    assert agent.challenge_calls == [pending.task_id]


def _request(store: ApprovalStore, path: str, summary: str):
    return store.request(
        "web:u1", "send_file", {"path": path}, summary=summary,
        rule_id="group-send", user_id="u1",
    )


@pytest.mark.asyncio
async def test_cards_must_be_approved_in_order(app_env):
    app, store, agent = app_env
    first = _request(store, "a.xlsx", "先生成报表")
    second = _request(store, "b.xlsx", "再发到群里")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        blocked = await client.post(
            "/api/chat/challenge",
            json={"task_id": second.task_id, "action": "confirm"},
            headers=_hdr("u1"),
        )
        assert blocked.json()["status"] == "blocked"
        assert blocked.json()["blocked_by"] == first.task_id
        assert "先生成报表" in blocked.json()["reply"]
        assert store.lookup(second.task_id).status == "pending"
        assert agent.challenge_calls == []

        # 前面的拒掉之后，后面的就能批
        await client.post(
            "/api/chat/challenge",
            json={"task_id": first.task_id, "action": "cancel"},
            headers=_hdr("u1"),
        )
        ok = await client.post(
            "/api/chat/challenge",
            json={"task_id": second.task_id, "action": "confirm"},
            headers=_hdr("u1"),
        )
    assert ok.json()["status"] == "approved"
    assert agent.challenge_calls == [second.task_id]


@pytest.mark.asyncio
async def test_resume_stream_feeds_result_back_once(app_env):
    app, store, _ = app_env
    gateway = app.state.message_gateway
    pending = _request(store, "a.xlsx", "把报表发到群里")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post(
            "/api/chat/challenge",
            json={"task_id": pending.task_id, "action": "confirm"},
            headers=_hdr("u1"),
        )
        # 别的会话拿不到这张卡片的续跑
        foreign = await client.post(
            "/api/chat/stream", json={"resume_task_id": pending.task_id}, headers=_hdr("u2")
        )
        assert foreign.status_code == 409

        resumed = await client.post(
            "/api/chat/stream",
            json={"message": "客户端的字不算", "resume_task_id": pending.task_id},
            headers=_hdr("u1"),
        )
        assert resumed.status_code == 200
        assert any(item["type"] == "done" for item in _events(resumed.text))

        again = await client.post(
            "/api/chat/stream", json={"resume_task_id": pending.task_id}, headers=_hdr("u1")
        )
        assert again.status_code == 409

    sent = gateway.messages[-1]
    assert sent.text == "[系统] 已执行：把报表发到群里"
    assert sent.metadata["transcript_text"] == "【已批准】把报表发到群里"
    assert sent.attachments == []


@pytest.mark.asyncio
async def test_web_approval_releases_next_card_without_waiting_for_resume(app_env):
    """网页端靠「同一会话只跑一轮」保证顺序；关页丢了续跑也不能把后面的卡片挡到超时。"""
    app, store, agent = app_env
    first = _request(store, "a.xlsx", "先生成报表")
    second = _request(store, "b.xlsx", "再发到群里")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for item in (first, second):
            resp = await client.post(
                "/api/chat/challenge",
                json={"task_id": item.task_id, "action": "confirm"},
                headers=_hdr("u1"),
            )
            assert resp.json()["status"] == "approved"
    assert agent.challenge_calls == [first.task_id, second.task_id]


@pytest.mark.asyncio
async def test_rejection_resume_tells_the_model_it_was_rejected(app_env):
    app, store, agent = app_env
    gateway = app.state.message_gateway
    pending = _request(store, "a.xlsx", "把报表发到群里")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        rejected = await client.post(
            "/api/chat/challenge",
            json={"task_id": pending.task_id, "action": "cancel"},
            headers=_hdr("u1"),
        )
        resumed = await client.post(
            "/api/chat/stream",
            json={"resume_task_id": rejected.json()["resume_task_id"]},
            headers=_hdr("u1"),
        )
    assert resumed.status_code == 200
    assert agent.challenge_calls == []  # 没执行
    sent = gateway.messages[-1]
    assert "用户拒绝了此前暂停的操作：把报表发到群里" in sent.text
    assert sent.metadata["transcript_text"] == "【已拒绝】把报表发到群里"



@pytest.mark.asyncio
async def test_pending_list_marks_queued_cards_not_ready(app_env):
    app, store, _ = app_env
    first = _request(store, "a.xlsx", "先生成报表")
    second = _request(store, "b.xlsx", "再发到群里")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        listed = (await client.get("/api/chat/challenges", headers=_hdr("u1"))).json()["items"]
        assert [(i["task_id"], i["ready"]) for i in listed] == [
            (first.task_id, True), (second.task_id, False)
        ]
        await client.post(
            "/api/chat/challenge",
            json={"task_id": first.task_id, "action": "cancel"},
            headers=_hdr("u1"),
        )
        listed = (await client.get("/api/chat/challenges", headers=_hdr("u1"))).json()["items"]
    assert [(i["task_id"], i["ready"]) for i in listed] == [(second.task_id, True)]


def test_sse_challenge_carries_ready_flag():
    store = ApprovalStore()
    a = _request(store, "a.xlsx", "a")
    b = _request(store, "b.xlsx", "b")

    def _ready(task_id):
        raw = router_chat._encode_challenge(json.dumps({"task_id": task_id, "summary": "x"}), store)
        return json.loads(raw[6:])["ready"]

    assert _ready(a.task_id) is True
    assert _ready(b.task_id) is False


@pytest.mark.asyncio
async def test_stop_during_approved_action_does_not_break_the_response(app_env):
    """批准的操作执行中点停止：停不下来就不假装停了，响应和续跑照常。"""
    import asyncio

    app, store, agent = app_env
    pending = store.request(
        "web:u1", "send_file", {"path": "a.xlsx"}, summary="把报表发到群里",
        rule_id="group-send", user_id="u1",
    )
    started = asyncio.Event()
    proceed = asyncio.Event()
    original = agent.execute_approved_action

    async def _slow(p):
        started.set()
        await proceed.wait()
        return await original(p)

    agent.execute_approved_action = _slow
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        decide = asyncio.create_task(client.post(
            "/api/chat/challenge",
            json={"task_id": pending.task_id, "action": "confirm"},
            headers=_hdr("u1"),
        ))
        await started.wait()
        stopped = await client.post("/api/chat/stop", headers=_hdr("u1"))
        assert stopped.json() == {"stopped": False, "reason": "approval_running"}
        # 执行期间会话仍被占着
        assert router_chat._running_turn("web:u1") is not None
        proceed.set()
        decided = await decide
    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == "approved" and body["resume_task_id"] == pending.task_id
    agent.mark_stop_requested.assert_not_called()
