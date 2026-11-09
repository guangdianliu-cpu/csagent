"""鉴权契约：这些用例锁的是安全边界，失败即代表出现越权风险。

背景：小窗页面（static/index.html）跑在最终用户的浏览器里，因此前端的任何闸门
都只是 UX。真正的边界只有两条，本文件逐条钉死：

1. 没有合法令牌，任何业务端点都进不去；
2. session_id 由服务端从验签身份派生，客户端传什么都无效。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.api.router_chat import router as chat_router
from csagent.api.router_files import router as files_router
from csagent.api.router_reports import router as reports_router
from csagent.api.router_skills import router as skills_router
from csagent.infra import auth
from csagent.memory.short_term import ShortTermMemory
from csagent.stream_events import AgentEventType, AgentStreamEvent

SECRET = "test-secret-that-is-long-enough-for-hmac-32"
OTHER_SECRET = "a-completely-different-secret-also-long-enough"


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.agent_auth_enabled = True
    settings.agent_shared_secret = SECRET
    settings.agent_token_ttl_seconds = 1800
    settings.agent_download_grant_ttl_seconds = 3600
    settings.agent_auth_dev_login = False
    settings.show_thinking_process = False
    settings.public_base_url = ""
    return settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from csagent.channels import workspace_uploads as wu

    monkeypatch.setattr(wu, "DEFAULT_UPLOADS_ROOT", tmp_path / "uploads")
    # workspace_relative() 用它算相对路径，不patch 会回传绝对路径导致 400
    monkeypatch.setattr(wu, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("csagent.api.router_files.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "csagent.api.router_files.DEFAULT_UPLOADS_ROOT", tmp_path / "uploads"
    )
    monkeypatch.setattr("csagent.api.attachments.PROJECT_ROOT", tmp_path)

    settings = _make_settings()
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_files.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_chat.get_settings", lambda: settings)
    # /api/skills/reload 走管理员鉴权（require_admin 读 router_admin 的 settings）
    monkeypatch.setattr("csagent.api.router_admin.get_settings", lambda: settings)

    app = FastAPI()
    for r in (chat_router, files_router, skills_router, reports_router):
        app.include_router(r)

    channel = MagicMock()
    channel.is_running = True
    manager = MagicMock()
    manager.get = MagicMock(return_value=channel)
    app.state.channel_manager = manager
    app.state.skill_discovery = MagicMock(scan=MagicMock(return_value=[]))

    # /api/chat/history 读的就是这份短期记忆，用真实实现才能验出会话隔离
    memory = ShortTermMemory()
    main_agent = MagicMock()
    main_agent.load_history = memory.load
    app.state.main_agent = main_agent

    seen: list[str] = []

    class _GW:
        def __init__(self, *a, **k):
            pass

        async def handle(self, message):
            seen.append(message.session_id)
            yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    monkeypatch.setattr("csagent.api.router_chat.MessageGateway", _GW)

    test_client = TestClient(app)
    test_client.seen_sessions = seen  # type: ignore[attr-defined]
    test_client.memory = memory  # type: ignore[attr-defined]
    return test_client


def _tok(uid: str, ttl: int = 600) -> str:
    return auth.issue_token(uid, ttl_seconds=ttl, secret=SECRET)


def _hdr(uid: str) -> dict:
    return {"Authorization": "Bearer " + _tok(uid)}


def _upload(client: TestClient, uid: str, name: str, body: bytes) -> str:
    resp = client.post(
        "/api/files/upload",
        headers=_hdr(uid),
        files={"file": (name, body, "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["workspace_path"]


# ---------------------------------------------------------------- 未授权拒绝


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/chat/stream"),
        ("get", "/api/chat/history"),
        ("get", "/api/chat/entries"),
        ("get", "/api/chat/suggest"),
        ("get", "/api/reports/rpt_00000000"),
        ("post", "/api/files/upload"),
        ("get", "/api/files/download"),
        ("get", "/api/files/preview"),
        ("get", "/api/skills"),
        ("post", "/api/skills/reload"),
    ],
)
def test_no_token_is_rejected(client: TestClient, method: str, path: str) -> None:
    """裸 curl 打任何业务端点都必须 401——这是本次改动的核心目的。"""
    if method == "post":
        resp = client.post(path, json={"message": "hi"})
    else:
        resp = client.get(path, params={"path": "x"})
    assert resp.status_code == 401, method.upper() + " " + path + " 未拦截"


@pytest.mark.parametrize(
    "bad",
    [
        "garbage",
        "v1.eyJ1aWQiOiJoIn0.AAAA",
        "v2.eyJ1aWQiOiJoIn0.AAAA",
        "",
    ],
)
def test_malformed_token_is_rejected(client: TestClient, bad: str) -> None:
    resp = client.get("/api/skills", headers={"Authorization": "Bearer " + bad})
    assert resp.status_code == 401


def test_expired_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/api/skills", headers=_hdr_expired())
    assert resp.status_code == 401


def _hdr_expired() -> dict:
    return {"Authorization": "Bearer " + auth.issue_token(
        "u1", ttl_seconds=-7200, secret=SECRET
    )}


def test_token_signed_with_other_secret_is_rejected(client: TestClient) -> None:
    forged = auth.issue_token("u1", ttl_seconds=600, secret=OTHER_SECRET)
    resp = client.get("/api/skills", headers={"Authorization": "Bearer " + forged})
    assert resp.status_code == 401


# ------------------------------------------------- session_id 由服务端派生


def test_session_id_comes_from_token_not_body(client: TestClient) -> None:
    """请求体里塞别人的 session_id 必须完全无效。"""
    with client.stream(
        "POST",
        "/api/chat/stream",
        headers=_hdr("zhang3"),
        json={"message": "hi", "session_id": "web:li4"},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())
    assert client.seen_sessions == ["web:zhang3"]  # type: ignore[attr-defined]


def test_history_returns_only_own_session(client: TestClient) -> None:
    """刷新后回填的历史只能来自令牌里的那个人。"""
    client.memory.append("web:zhang3", "我的问题", "我的回答")  # type: ignore[attr-defined]
    client.memory.append("web:li4", "别人的问题", "别人的回答")  # type: ignore[attr-defined]

    resp = client.get("/api/chat/history", headers=_hdr("zhang3"))
    assert resp.status_code == 200
    assert [m["content"] for m in resp.json()["messages"]] == ["我的问题", "我的回答"]


def test_history_ignores_client_supplied_session(client: TestClient) -> None:
    """查询串里塞别人的 session_id 必须完全无效。"""
    client.memory.append("web:li4", "别人的问题", "别人的回答")  # type: ignore[attr-defined]

    resp = client.get(
        "/api/chat/history",
        params={"session_id": "web:li4"},
        headers=_hdr("zhang3"),
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_upload_lands_in_own_session_dir(client: TestClient) -> None:
    rel = _upload(client, "zhang3", "a.txt", b"mine")
    assert "web_zhang3" in rel


# ---------------------------------------------------------------- 越权下载


def test_cannot_download_other_users_file(client: TestClient) -> None:
    victim = _upload(client, "li4", "secret.txt", b"li4-private")
    resp = client.get(
        "/api/files/download", params={"path": victim}, headers=_hdr("zhang3")
    )
    assert resp.status_code == 404


def test_traversal_cannot_escape_own_session(client: TestClient) -> None:
    """字符串前缀看着是自己的，解析后落在别人目录——必须拦住。"""
    _upload(client, "li4", "secret.txt", b"li4-private")
    _upload(client, "zhang3", "mine.txt", b"x")
    sneaky = "uploads/web_zhang3/inbound/../../web_li4/inbound/secret.txt"
    resp = client.get(
        "/api/files/download", params={"path": sneaky}, headers=_hdr("zhang3")
    )
    assert resp.status_code == 404


def test_owner_can_download_own_file(client: TestClient) -> None:
    rel = _upload(client, "zhang3", "mine.txt", b"mine-content")
    resp = client.get(
        "/api/files/download", params={"path": rel}, headers=_hdr("zhang3")
    )
    assert resp.status_code == 200
    assert resp.content == b"mine-content"


def test_sheet_preview_follows_download_rules(client: TestClient) -> None:
    """预览与下载同一套鉴权：自己的能看，别人的 404，授权链接免头。"""
    rel = _upload(client, "zhang3", "t.csv", "名称,收益\nA,+3.2%\n".encode("utf-8"))
    ok = client.get("/api/files/preview", params={"path": rel}, headers=_hdr("zhang3"))
    assert ok.status_code == 200
    assert ok.json()["sheets"][0]["rows"] == [["名称", "收益"], ["A", "+3.2%"]]
    assert client.get("/api/files/preview", params={"path": rel}, headers=_hdr("li4")).status_code == 404
    grant = auth.issue_download_grant(rel, ttl_seconds=600, secret=SECRET)
    assert client.get("/api/files/preview", params={"path": rel, "dl": grant}).status_code == 200


def test_preview_rejects_other_formats(client: TestClient) -> None:
    rel = _upload(client, "zhang3", "a.txt", b"x")
    resp = client.get("/api/files/preview", params={"path": rel}, headers=_hdr("zhang3"))
    assert resp.status_code == 415


# ------------------------------------------------------------ 下载授权链接


def test_download_grant_works_without_any_header(client: TestClient) -> None:
    """企微把链接发到聊天里，用户点击时不可能带 Authorization 头。"""
    rel = _upload(client, "zhang3", "report.txt", b"deliverable")
    grant = auth.issue_download_grant(rel, ttl_seconds=600, secret=SECRET)
    resp = client.get("/api/files/download", params={"path": rel, "dl": grant})
    assert resp.status_code == 200
    assert resp.content == b"deliverable"


def test_grant_is_scoped_to_one_path(client: TestClient) -> None:
    victim = _upload(client, "li4", "secret.txt", b"li4-private")
    mine = _upload(client, "zhang3", "mine.txt", b"x")
    grant = auth.issue_download_grant(mine, ttl_seconds=600, secret=SECRET)
    resp = client.get("/api/files/download", params={"path": victim, "dl": grant})
    assert resp.status_code == 403


def test_expired_grant_is_rejected(client: TestClient) -> None:
    rel = _upload(client, "zhang3", "mine.txt", b"x")
    grant = auth.issue_download_grant(rel, ttl_seconds=-7200, secret=SECRET)
    resp = client.get("/api/files/download", params={"path": rel, "dl": grant})
    assert resp.status_code == 401


def test_id_token_cannot_be_used_as_download_grant(client: TestClient) -> None:
    """两类令牌同一把密钥签，必须靠 typ 隔离，否则一条能换另一条。"""
    rel = _upload(client, "zhang3", "mine.txt", b"x")
    resp = client.get(
        "/api/files/download", params={"path": rel, "dl": _tok("zhang3")}
    )
    assert resp.status_code == 401


def test_download_grant_cannot_be_used_as_id_token(client: TestClient) -> None:
    grant = auth.issue_download_grant(
        "uploads/web_zhang3/inbound/a.txt", ttl_seconds=600, secret=SECRET
    )
    resp = client.get("/api/skills", headers={"Authorization": "Bearer " + grant})
    assert resp.status_code == 401


def test_grant_cannot_reach_files_outside_session_dirs(
    client: TestClient, tmp_path: Path
) -> None:
    """持有签名密钥的一方伪造授权，也取不到会话目录之外的文件（记忆库、审计、配置）。"""
    (tmp_path / "data" / "audit").mkdir(parents=True)
    (tmp_path / "data" / "audit" / "sessions-2026-09-28.jsonl").write_text("{}", "utf-8")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "base.yaml").write_text("a: 1", "utf-8")
    for rel in ("data/audit/sessions-2026-09-28.jsonl", "configs/base.yaml"):
        grant = auth.issue_download_grant(rel, ttl_seconds=600, secret=SECRET)
        resp = client.get("/api/files/download", params={"path": rel, "dl": grant})
        assert resp.status_code == 403, rel


def test_grant_downloads_are_security_audited(
    client: TestClient, _isolate_security_state: Path
) -> None:
    import json

    rel = _upload(client, "zhang3", "report.txt", b"deliverable")
    grant = auth.issue_download_grant(rel, ttl_seconds=600, secret=SECRET)
    assert client.get("/api/files/download", params={"path": rel, "dl": grant}).status_code == 200
    bad = client.get("/api/files/download", params={"path": rel, "dl": "v1.x.y"})
    assert bad.status_code == 401

    events = [
        json.loads(line)
        for path in _isolate_security_state.glob("security-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    outcomes = [(e["event"], e["outcome"]) for e in events]
    assert ("download_via_grant", "success") in outcomes
    assert ("download_via_grant", "denied") in outcomes
    assert all(grant not in json.dumps(e) for e in events)


def test_identity_token_cannot_reload_skills(client: TestClient) -> None:
    """热重载改的是整个进程的 Skill 集合，普通用户令牌不行，只认管理员令牌。"""
    resp = client.post("/api/skills/reload", headers=_hdr("zhang3"))
    assert resp.status_code == 401


# ------------------------------------------------------------ dev-token 开关


def test_dev_token_is_404_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产开关关闭时，联调后门必须完全不可用。"""
    from csagent.api.router_auth import router as auth_router

    settings = _make_settings()
    settings.agent_auth_dev_login = False
    monkeypatch.setattr("csagent.api.router_auth.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(auth_router)
    resp = TestClient(app).post("/api/auth/dev-token", json={"user_id": "anyone"})
    assert resp.status_code == 404


def test_dev_token_issues_usable_token_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csagent.api.router_auth import router as auth_router

    settings = _make_settings()
    settings.agent_auth_dev_login = True
    monkeypatch.setattr("csagent.api.router_auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(auth_router)
    resp = TestClient(app).post("/api/auth/dev-token", json={"user_id": "u9"})
    assert resp.status_code == 200
    principal = auth.verify_token(resp.json()["token"], secret=SECRET)
    assert principal.session_id == "web:u9"


# ---------------------------------------------------------------- 报告卡片


def _content():
    from csagent.reports.model import ReportContent

    return ReportContent(
        title="对比",
        summary="一句话",
        blocks=[
            {"type": "markdown", "text": "## 正文"},
            {
                "type": "entities",
                "items": [{"name": "甲"}, {"name": "乙"}],
                "metrics": [{"label": "收益", "values": [1, 2], "best": "max"}],
            },
        ],
    )


def test_report_only_readable_by_own_session(client: TestClient) -> None:
    from csagent.reports import store

    report, _ = store.save("web:zhang3", _content())
    store.save("web:zhang3", _content(), report_id=report.report_id)

    own = client.get(f"/api/reports/{report.report_id}", headers=_hdr("zhang3"))
    assert own.status_code == 200
    assert own.json()["report"]["version"] == 2
    assert own.json()["versions"] == [1, 2]
    # 记分卡的胜出格和「各赢几项」由服务端算好
    assert own.json()["highlights"] == {"1": [[0, 1]]}
    assert own.json()["tallies"] == {"1": {"wins": [["甲", 0], ["乙", 1]], "ties": 0}}
    v1 = client.get(f"/api/reports/{report.report_id}?v=1", headers=_hdr("zhang3"))
    assert v1.json()["report"]["version"] == 1

    other = client.get(f"/api/reports/{report.report_id}", headers=_hdr("li4"))
    assert other.status_code == 404
    assert client.get("/api/reports/..%2Fx", headers=_hdr("zhang3")).status_code == 404


def test_stream_pushes_report_card_before_done(client: TestClient, monkeypatch) -> None:
    import json

    from csagent.channels.outbound_reports import OutboundReport, enqueue_outbound_report

    class _GW:
        def __init__(self, *a, **k):
            pass

        async def handle(self, message):
            yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="结论")
            enqueue_outbound_report(
                message.session_id,
                OutboundReport(report_id="rpt_0000abcd", version=1, title="对比", summary="摘要"),
            )

    monkeypatch.setattr("csagent.api.router_chat.MessageGateway", _GW)
    with client.stream(
        "POST", "/api/chat/stream", headers=_hdr("zhang3"), json={"message": "hi"}
    ) as resp:
        body = "".join(resp.iter_text())
    events = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    types = [e["type"] for e in events]
    assert types[-2:] == ["report", "done"]
    assert events[-2]["report_id"] == "rpt_0000abcd"


def test_report_export_own_session_only(client: TestClient) -> None:
    from csagent.reports import store
    from csagent.reports.model import ReportContent

    report, _ = store.save(
        "web:zhang3",
        ReportContent(
            title="对比",
            summary="一句话",
            blocks=[{"type": "table", "columns": ["a"], "rows": [["1"]]}],
        ),
    )
    url = f"/api/reports/{report.report_id}/export?fmt=xlsx"
    own = client.get(url, headers=_hdr("zhang3"))
    assert own.status_code == 200, own.text
    assert own.json()["filename"].endswith("_v1.xlsx")
    assert "dl=" in own.json()["url"]
    dl = client.get(own.json()["url"])
    assert dl.status_code == 200
    assert client.get(url, headers=_hdr("li4")).status_code == 404
    bad = client.get(f"/api/reports/{report.report_id}/export?fmt=docx", headers=_hdr("zhang3"))
    assert bad.status_code == 422
