"""发送机主动连本服务（模式 B）：hello → 推消息（带原图）→ 取回复 → 回报，游标照旧处理完才前进。

发送机用 httpx 的 ASGI 传输模拟，和桥跑在同一个事件循环里（发件箱的 Future 不跨循环）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from csagent.wechat import hub as hub_mod
from csagent.wechat.bridge import WeChatAtBridge, WeChatConfig
from csagent.wechat.hub import HubLink
from tests.test_wechat_bridge import PNG, FakeGateway, FakeSettings, _msg

TOKEN = "link-token-0123456789-abcdef"
H = {"Authorization": f"Bearer {TOKEN}"}
LINK = "/api/wechat/link"


@pytest.fixture()
async def env(tmp_path: Path):
    hub = HubLink(TOKEN)
    app = FastAPI()
    app.include_router(hub.router())
    gateway = FakeGateway()
    bridge = WeChatAtBridge(
        settings=FakeSettings(),  # type: ignore[arg-type]
        message_gateway=gateway,  # type: ignore[arg-type]
        config=WeChatConfig.from_dict({"reply_mode": "auto", "ack_after_seconds": 0, "poll_wait_seconds": 0.05}),
        link=hub,
        cursor_path=tmp_path / "cursor.json",
    )
    await bridge.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cs") as client:
        yield hub, bridge, gateway, client, tmp_path
    await bridge.stop()


async def _push(client: httpx.AsyncClient, inbox: str, messages: list[dict], images: dict[int, bytes] | None = None):
    files = [(f"image_{mid}", (f"{mid}.png", data, "image/png")) for mid, data in (images or {}).items()]
    return await client.post(
        f"{LINK}/messages",
        data={"inbox": inbox, "messages": json.dumps(messages, ensure_ascii=False)},
        files=files or None,
        headers=H,
    )


async def _take_job(client: httpx.AsyncClient, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"{LINK}/outbox", params={"wait": 0.2}, headers=H)).json()
        if body["jobs"]:
            return body["jobs"][0] | {"session": body["session"]}
    raise AssertionError("发件箱里一直没有任务")


async def _until(pred, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等不到条件成立")
        await asyncio.sleep(0.01)


async def test_requires_token(env):
    _, _, _, client, _ = env
    assert (await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 0})).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get(f"{LINK}/outbox", headers=bad)).status_code == 401


async def test_full_round_trip(env):
    hub, bridge, gateway, client, tmp_path = env
    # 首次接入：从发送机当前最新的 id 开始，不补历史
    r = (await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 5, "instance": "PC-01"}, headers=H)).json()
    assert r["ok"] and r["cursor"] == 5 and r["session"] == hub.session

    quote = {"type": "image", "sender": "测试"}
    r = await _push(client, "ib1", [_msg(6, quote=quote)], images={6: PNG})
    assert r.json() == {"ok": True, "accepted": [6], "session": hub.session}

    job = await _take_job(client)
    assert job["group"] == "测试群2"
    assert job["items"] == [{"type": "text", "text": "@测试 请重启一下客户端"}]
    assert job["request_id"] == "wechat-at-ib1-6-reply" and job["files"] == []
    # 引用的原图随消息推上来，作为图片附件交给 Agent
    [att] = gateway.messages[0].attachments
    assert Path(att.local_path).read_bytes() == PNG and att.metadata["from_quote"] is True

    # 没回报之前游标不动；回报后前进并落盘
    assert bridge.status["cursor"] == 5
    assert (await client.post(f"{LINK}/outbox/{job['id']}/result", json={"ok": True}, headers=H)).json() == {"ok": True}
    await _until(lambda: bridge.status["cursor"] == 6)
    assert json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8")) == {"inbox": "ib1", "cursor": 6}
    assert bridge.status["link"]["online"] and bridge.status["link"]["instance"] == "PC-01"

    # 发送机重连：拿到处理完的位置；重推的旧消息被忽略
    r = (await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 6}, headers=H)).json()
    assert r["cursor"] == 6
    await _push(client, "ib1", [_msg(6)])
    await asyncio.sleep(0.2)
    assert len(gateway.messages) == 1
    # 结果重复回报 / 任务不存在
    assert (await client.post(f"{LINK}/outbox/{job['id']}/result", json={"ok": True}, headers=H)).status_code == 404


async def test_push_before_hello_or_wrong_inbox_is_rejected(env):
    _, _, _, client, _ = env
    assert (await _push(client, "ib1", [_msg(1)])).status_code == 409
    await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 0}, headers=H)
    assert (await _push(client, "other", [_msg(1)])).status_code == 409


async def test_new_inbox_resets_cursor(env):
    """发送机删库重建（标识变了）：游标跟着换库，从新库的最新 id 开始。"""
    _, bridge, gateway, client, _ = env
    await client.post(f"{LINK}/hello", json={"inbox": "old", "last_id": 50}, headers=H)
    r = (await client.post(f"{LINK}/hello", json={"inbox": "new", "last_id": 0}, headers=H)).json()
    assert r["cursor"] == 0 and bridge.status["inbox"] == "new"
    await _push(client, "new", [_msg(1)])
    job = await _take_job(client)
    assert job["request_id"] == "wechat-at-new-1-reply"


async def test_unreported_job_is_redelivered(env, monkeypatch):
    """交出去的任务一直没回报（回报丢了、发送机重启了）就再交一次；发送机按 request_id 去重。"""
    monkeypatch.setattr(hub_mod, "REDELIVER_AFTER_SECONDS", 0.1)
    _, _, _, client, _ = env
    await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 0}, headers=H)
    await _push(client, "ib1", [_msg(1)])
    first = await _take_job(client)
    await asyncio.sleep(0.15)
    again = await _take_job(client)
    assert again["id"] == first["id"] and again["request_id"] == first["request_id"]


async def test_sender_offline_job_expires(env, monkeypatch):
    """发送机一直不来取：回复作废、桥收到失败，游标照样前进（和连不上发送机时一样不重试）。"""
    monkeypatch.setattr(hub_mod, "JOB_TTL_SECONDS", 0.1)
    hub, bridge, _, client, _ = env
    await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 0}, headers=H)
    await _push(client, "ib1", [_msg(1)])
    await _until(lambda: hub.status["outbox"] == 1)
    await asyncio.sleep(0.15)
    await client.get(f"{LINK}/outbox", params={"wait": 0}, headers=H)  # 触发清理
    await _until(lambda: bridge.status["cursor"] == 1)
    assert hub.status["outbox"] == 0


async def test_reply_files_are_downloadable(env):
    hub, _, _, client, _ = env
    await client.post(f"{LINK}/hello", json={"inbox": "ib1", "last_id": 0}, headers=H)
    task = asyncio.create_task(
        hub.send("测试群2", [{"type": "text", "text": "见附件"}, {"type": "file", "file": 0}], files=[("日报.xlsx", b"XLSX")], request_id="r1")
    )
    job = await _take_job(client)
    assert job["files"] == ["日报.xlsx"]
    r = await client.get(f"{LINK}/outbox/{job['id']}/files/0", headers=H)
    assert r.status_code == 200 and r.content == b"XLSX"
    assert (await client.get(f"{LINK}/outbox/{job['id']}/files/1", headers=H)).status_code == 404
    await client.post(f"{LINK}/outbox/{job['id']}/result", json={"ok": False, "error": "视觉确认未通过"}, headers=H)
    assert await task == {"ok": False, "error": "视觉确认未通过"}
