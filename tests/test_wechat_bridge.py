"""微信群通道：拉取 → Agent → 经发送机回复（假发送机、假网关，不碰网络）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from csagent.channels.models import UnifiedMessage
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.wechat.bridge import (
    WeChatAtBridge,
    WeChatConfig,
    compose_reply,
    image_type,
    markdown_to_plain,
    strip_mention,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32


def _now(minutes_ago: float = 0) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _msg(mid: int, **kw) -> dict:
    base = {
        "id": mid,
        "chat": "测试群2",
        "sender": "测试",
        "text": "@伯温 这个报错怎么处理？",
        "at": "伯温",
        "quote": None,
        "time_label": "15:06",
        "read_at": _now(),
        "warnings": [],
    }
    return base | kw


class FakeClient:
    """假的发送机接入点（顶替 HubLink）：发送机先 hello（桥绑定的回调给出游标），再按游标给消息。

    hello 在第一次 pull 时发生；``inbox`` 改了就再 hello 一次，和真发送机发现库变了一样。
    """

    mode = "hub"
    status = {"mode": "hub", "online": True}

    def __init__(self, batches: list[list[dict]] | None = None, latest: int = 0, inbox: str = "inbox001") -> None:
        self.store = [m for batch in batches or [] for m in batch]
        self.latest = latest
        self.inbox = inbox
        self.sent: list[dict] = []
        self.hellos: list[tuple[str, int]] = []  # (库, 拿到的游标)
        self.downloads: list[str] = []
        self.closed = False
        self._on_hello = None
        self._hello_inbox: str | None = None

    def bind(self, on_hello) -> None:
        self._on_hello = on_hello

    async def pull(self, after: int, *, wait: float, limit: int = 20):
        """长轮询：有就立刻给，没有就等到 wait 用完返回空。"""
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            if self._hello_inbox != self.inbox:
                self._hello_inbox = self.inbox
                self.hellos.append((self.inbox, self._on_hello(self.inbox, self.latest)))
            ready = [m for m in self.store if m["id"] > after][:limit]
            if ready:
                return ready, ready[-1]["id"], self.inbox
            if asyncio.get_running_loop().time() >= deadline:
                return [], after, self.inbox
            await asyncio.sleep(0.01)

    async def download(self, url: str) -> bytes:
        self.downloads.append(url)
        if "missing" in url:
            raise LookupError("消息的原图不在了")
        return JPEG if "jpeg" in url else PNG

    async def send(self, group, items, *, files=None, request_id, wait=True):
        self.sent.append({"group": group, "items": items, "files": files or [], "request_id": request_id, "wait": wait})
        return {"ok": True}

    async def close(self) -> None:
        self.closed = True


class FakeGateway:
    def __init__(self, answer: str = "请重启一下客户端", delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.messages: list[UnifiedMessage] = []

    async def handle(self, message: UnifiedMessage):
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        yield AgentStreamEvent(event_type=AgentEventType.PROGRESS, content="思考中")
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content=self.answer)


class FakeSettings:
    wechat_link_token = "x" * 40
    react_overall_timeout_seconds = 0


def _bridge(tmp_path: Path, client: FakeClient, gateway: FakeGateway, **cfg) -> WeChatAtBridge:
    config = WeChatConfig.from_dict({"reply_mode": "auto", "ack_after_seconds": 0, "poll_wait_seconds": 0.05} | cfg)
    return WeChatAtBridge(
        settings=FakeSettings(),  # type: ignore[arg-type]
        message_gateway=gateway,  # type: ignore[arg-type]
        config=config,
        link=client,  # type: ignore[arg-type]
        cursor_path=tmp_path / "cursor.json",
    )


async def _drain(bridge: WeChatAtBridge, client: FakeClient, expect_done: int, timeout: float = 3.0) -> None:
    """跑拉取循环，直到游标前进到 expect_done。"""
    await bridge.start()
    deadline = asyncio.get_running_loop().time() + timeout
    while bridge.status["cursor"] < expect_done:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"cursor 停在 {bridge.status['cursor']}")
        await asyncio.sleep(0.01)
    await bridge.stop()


# ---------- 文本 ----------
def test_markdown_to_plain():
    md = "## 结论\n**先重启**客户端，详见 [说明](https://x.cn/a)。\n- 第一步\n- 第二步\n`code`\n|a|b|\n|---|---|\n|1|2|"
    assert markdown_to_plain(md) == "结论\n先重启客户端，详见 说明（https://x.cn/a）。\n• 第一步\n• 第二步\ncode\n|a|b|\n|1|2|"


def test_strip_mention_and_compose_reply():
    assert strip_mention("@伯温 这个报错怎么处理？", "伯温") == "这个报错怎么处理？"
    assert strip_mention("这个报错怎么处理@伯温", "伯温") == "这个报错怎么处理"
    assert strip_mention("@伯温", "伯温") == ""
    # OCR 吃掉了 @ 后面的特殊空格，名字后面紧跟正文
    assert strip_mention("@伯温这个报错", "伯温") == "这个报错"
    assert compose_reply("测试", "**好的**", limit=100) == "@测试 好的"
    assert compose_reply("测试", "长" * 10, limit=4) == "@测试 长长长长……（内容较长，已截断）"


def test_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        WeChatConfig.from_dict({"reply_mode": "yolo"})
    assert WeChatConfig.from_dict({"unknown": 1}).reply_mode == "draft"


# ---------- 流程 ----------
async def test_reply_in_group_with_mention(tmp_path):
    client = FakeClient([[_msg(7)]], latest=6)
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 7)

    [message] = gateway.messages
    # 整群一个会话，发言人放 metadata，由网关标进正文
    assert message.session_id == "wechat:group:测试群2"
    assert message.metadata == {"speaker": "测试"}
    assert message.text == "这个报错怎么处理？"
    assert message.principal == {"user_id": "测试", "auth_method": "wechat", "chat_type": "group", "chat_id": "测试群2"}
    [sent] = client.sent
    assert sent["group"] == "测试群2"
    assert sent["items"] == [{"type": "text", "text": "@测试 请重启一下客户端"}]
    # 幂等键带上消息库标识：库重建后 id 从 1 重来也不会撞上以前的回复
    assert sent["request_id"] == "wechat-at-inbox001-7-reply"
    # 首次接入：hello 时认出消息库，从发送机当前最新 id 开始；处理完游标按库落盘
    assert client.hellos == [("inbox001", 6)]
    assert json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8")) == {"inbox": "inbox001", "cursor": 7}
    assert client.closed


async def test_resume_from_saved_cursor(tmp_path):
    (tmp_path / "cursor.json").write_text(json.dumps({"inbox": "inbox001", "cursor": 3}), encoding="utf-8")
    client = FakeClient([[_msg(4)]], latest=99)
    bridge = _bridge(tmp_path, client, FakeGateway())
    await _drain(bridge, client, 4)
    assert client.hellos == [("inbox001", 3)]


@pytest.mark.parametrize("saved", [{"inbox": "rebuilt", "cursor": 3}, {"sender_url": "http://10.0.0.1:8090", "cursor": 3}])
async def test_cursor_of_other_inbox_is_ignored(tmp_path, saved):
    """别的消息库（发送机删库重建过）或老格式的游标：按首次接入从最新开始。"""
    (tmp_path / "cursor.json").write_text(json.dumps(saved), encoding="utf-8")
    client = FakeClient([[_msg(51)]], latest=50)
    bridge = _bridge(tmp_path, client, FakeGateway())
    await _drain(bridge, client, 51)
    assert client.hellos == [("inbox001", 50)]


@pytest.mark.parametrize(
    ("msg", "reason"),
    [
        (_msg(1, warnings=["low_confidence"]), "没把握"),
        (_msg(1, warnings=["image_missing"]), "没把握"),
        (_msg(1, sender=""), "提问人"),
        (_msg(1, read_at=_now(minutes_ago=90)), "太旧"),
    ],
)
async def test_unsure_or_stale_messages_are_not_answered(tmp_path, msg, reason):
    client = FakeClient([[msg]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 1)
    assert not gateway.messages and not client.sent


async def test_first_read_warning_still_answered(tmp_path):
    client = FakeClient([[_msg(1, warnings=["first_read"])]])
    bridge = _bridge(tmp_path, client, FakeGateway())
    await _drain(bridge, client, 1)
    assert len(client.sent) == 1


async def test_rate_limit_per_group(tmp_path):
    batch = [_msg(i) for i in range(1, 5)] + [_msg(5, chat="测试群4")]
    client = FakeClient([batch])
    bridge = _bridge(tmp_path, client, FakeGateway(), max_replies_per_minute=3)
    await _drain(bridge, client, 5)
    groups = [s["group"] for s in client.sent]
    assert groups.count("测试群2") == 3 and groups.count("测试群4") == 1


async def test_draft_mode_does_not_reply_in_group(tmp_path):
    client = FakeClient([[_msg(1)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway, reply_mode="draft")
    await _drain(bridge, client, 1)
    assert gateway.messages and not client.sent


async def test_draft_mode_posts_to_review_group(tmp_path):
    client = FakeClient([[_msg(1)]])
    bridge = _bridge(tmp_path, client, FakeGateway(), reply_mode="draft", review_group="客服审核群")
    await _drain(bridge, client, 1)
    [sent] = client.sent
    assert sent["group"] == "客服审核群"
    assert sent["items"][0]["text"] == "【待审核】测试群2 / 测试：这个报错怎么处理？\n——\n@测试 请重启一下客户端"


async def test_off_mode_only_records(tmp_path):
    client = FakeClient([[_msg(1)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway, reply_mode="off")
    await _drain(bridge, client, 1)
    assert not gateway.messages and not client.sent


async def test_quoted_screenshot_becomes_image_attachment(tmp_path):
    quote = {"type": "image", "sender": "测试", "image_url": "link://9"}
    client = FakeClient([[_msg(9, quote=quote)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 9)
    [message] = gateway.messages
    [att] = message.attachments
    assert att.type.value == "image" and att.metadata["from_quote"] is True
    assert Path(att.local_path).read_bytes() == PNG
    assert (att.filename, att.mime_type) == ("引用截图_9.png", "image/png")
    assert client.downloads == [quote["image_url"]]


async def test_next_question_image_waits_for_its_turn(tmp_path, monkeypatch):
    """同群连着问：前一条还在答时，后一条引用的原图不能先放进会话目录，否则 Agent 翻目录会拿它答前一条。"""
    import csagent.wechat.bridge as bridge_mod

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    monkeypatch.setattr(bridge_mod, "session_inbound_dir", lambda session_id: inbound)
    quote = {"type": "image", "sender": "测试", "image_url": "link://9"}
    client = FakeClient([[_msg(8, text="@伯温 这是什么"), _msg(9, text="@伯温 这是什么", quote=quote)]])
    seen: list[list[str]] = []

    class Gateway(FakeGateway):
        async def handle(self, message):
            await asyncio.sleep(0.05)  # 让后一条先跑到它能跑到的地方
            seen.append(sorted(p.name for p in inbound.iterdir()))
            async for event in super().handle(message):
                yield event

    bridge = _bridge(tmp_path, client, Gateway())
    await _drain(bridge, client, 9)
    assert seen == [[], ["引用截图_9.png"]]


async def test_quoted_photo_keeps_its_format(tmp_path):
    """手机发的原图是 JPEG：文件名和交给模型的 MIME 跟着内容走，不能一律当 PNG。"""
    quote = {"type": "image", "sender": "测试", "image_url": "link://jpeg"}
    client = FakeClient([[_msg(9, quote=quote)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 9)
    [att] = gateway.messages[0].attachments
    assert (att.filename, att.mime_type) == ("引用截图_9.jpg", "image/jpeg")
    assert Path(att.local_path).read_bytes() == JPEG


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, (".png", "image/png")),
        (JPEG, (".jpg", "image/jpeg")),
        (b"GIF89a" + b"0" * 8, (".gif", "image/gif")),
        (b"RIFF" + b"0" * 4 + b"WEBPVP8 ", (".webp", "image/webp")),
        (b"what is this", (".png", "image/png")),
    ],
)
def test_image_type_by_magic(data, expected):
    assert image_type(data) == expected


async def test_quoted_screenshot_download_failure_is_told_to_agent(tmp_path):
    quote = {"type": "image", "sender": "测试", "image_url": "link://missing"}
    client = FakeClient([[_msg(9, quote=quote)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 9)
    [att] = gateway.messages[0].attachments
    assert att.local_path is None and "原图不在了" in att.metadata["download_error"]


async def test_quoted_text_passed_as_quote(tmp_path):
    quote = {"type": "text", "sender": "测试", "text": "今日净值1.23"}
    client = FakeClient([[_msg(2, text="@伯温 这个对吗", quote=quote)]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 2)
    assert gateway.messages[0].quote_text == "今日净值1.23"


async def test_only_mention_gets_guide(tmp_path):
    client = FakeClient([[_msg(3, text="@伯温")]])
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await _drain(bridge, client, 3)
    assert not gateway.messages
    [sent] = client.sent
    assert sent["items"][0]["text"].startswith("@测试 ")


async def test_slow_answer_sends_ack_first(tmp_path):
    client = FakeClient([[_msg(5)]])
    bridge = _bridge(tmp_path, client, FakeGateway(delay=0.3), ack_after_seconds=0.05)
    await _drain(bridge, client, 5)
    assert [s["request_id"] for s in client.sent] == ["wechat-at-inbox001-5-ack", "wechat-at-inbox001-5-reply"]
    assert client.sent[0]["items"][0]["text"] == "@测试 已收到，正在处理"
    assert client.sent[0]["wait"] is False


async def test_fast_answer_skips_ack(tmp_path):
    client = FakeClient([[_msg(5)]])
    bridge = _bridge(tmp_path, client, FakeGateway(), ack_after_seconds=0.5)
    await _drain(bridge, client, 5)
    assert [s["request_id"] for s in client.sent] == ["wechat-at-inbox001-5-reply"]


async def test_cursor_waits_for_earlier_message(tmp_path):
    """后到的先处理完，游标也要等前面那条做完才前进，重启时不会把没答完的跳过去。"""

    class SlowFirst(FakeGateway):
        async def handle(self, message):
            if "慢" in message.text:
                await asyncio.sleep(0.3)
            async for ev in super().handle(message):
                yield ev

    client = FakeClient([[_msg(1, text="@伯温 慢问题"), _msg(2, chat="测试群4")]])
    bridge = _bridge(tmp_path, client, SlowFirst())
    await bridge.start()
    for _ in range(100):
        if any(s["request_id"] == "wechat-at-inbox001-2-reply" for s in client.sent):
            break
        await asyncio.sleep(0.01)
    assert bridge.status["cursor"] == 0
    for _ in range(100):
        if bridge.status["cursor"] == 2:
            break
        await asyncio.sleep(0.01)
    assert bridge.status["cursor"] == 2
    await bridge.stop()


async def test_sender_inbox_rebuilt_while_running(tmp_path):
    """发送机删库重建：id 从 1 重来、标识变了。游标跟着换到新库，不会一直等 id 超过旧游标。"""
    client = FakeClient([[_msg(40)]], latest=39, inbox="oldinbox")
    gateway = FakeGateway()
    bridge = _bridge(tmp_path, client, gateway)
    await bridge.start()
    for _ in range(200):
        if bridge.status["cursor"] == 40:
            break
        await asyncio.sleep(0.01)
    assert bridge.status["cursor"] == 40
    # 新库：最新 id 是 0，随后来了 id=1 的新消息
    client.inbox, client.latest, client.store = "newinbox", 0, [_msg(1, text="@伯温 新库的第一条")]
    for _ in range(300):
        if bridge.status["inbox"] == "newinbox" and bridge.status["cursor"] == 1:
            break
        await asyncio.sleep(0.01)
    await bridge.stop()
    assert bridge.status["inbox"] == "newinbox" and bridge.status["cursor"] == 1
    assert [m.text for m in gateway.messages] == ["这个报错怎么处理？", "新库的第一条"]
    assert [s["request_id"] for s in client.sent] == ["wechat-at-oldinbox-40-reply", "wechat-at-newinbox-1-reply"]


class _TokenSettings:
    def __init__(self, token: str) -> None:
        self.wechat_link_token = token


def test_make_hub_requires_long_token():
    from csagent.wechat.bridge import make_hub
    from csagent.wechat.hub import HubLink

    assert isinstance(make_hub(_TokenSettings("x" * 32)), HubLink)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="至少"):
        make_hub(_TokenSettings("short"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="没有配置"):
        make_hub(_TokenSettings(""))  # type: ignore[arg-type]
