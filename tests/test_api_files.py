"""HTTP 文件上传/下载与聊天附件路径。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.api.attachments import attachments_from_workspace_paths
from csagent.api.router_chat import router as chat_router
from csagent.api.router_files import build_download_url, router as files_router
from csagent.channels.models import AttachmentType
from csagent.infra.auth import Principal, optional_principal, require_principal
from csagent.stream_events import AgentEventType, AgentStreamEvent


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from csagent.channels import workspace_uploads as wu

    monkeypatch.setattr(wu, "DEFAULT_UPLOADS_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(wu, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("csagent.api.router_files.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("csagent.api.router_files.DEFAULT_UPLOADS_ROOT", tmp_path / "uploads")
    monkeypatch.setattr("csagent.api.attachments.PROJECT_ROOT", tmp_path)

    app = FastAPI()
    app.include_router(files_router)
    app.include_router(chat_router)

    # 覆盖鉴权依赖：本文件测的是文件读写逻辑，鉴权契约见 test_api_auth.py。
    # session_id 由 Principal 派生，因此固定为 web:sess-http。
    # download 用的是 optional_principal（它还接受下载授权），两个都要覆盖。
    _principal = Principal(user_id="sess-http")
    app.dependency_overrides[require_principal] = lambda: _principal
    app.dependency_overrides[optional_principal] = lambda: _principal

    channel = MagicMock()
    channel.is_running = True
    manager = MagicMock()
    manager.get = MagicMock(return_value=channel)
    app.state.channel_manager = manager

    async def _stream(**kwargs):
        yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="ok")

    main_agent = MagicMock()
    main_agent.handle_message_stream = _stream
    app.state.main_agent = main_agent

    # MessageGateway 会新建；用 monkeypatch 让 chat 走假 gateway
    class _GW:
        def __init__(self, *a, **k):
            pass

        async def handle(self, message):
            assert message.attachments
            assert message.attachments[0].local_path
            yield AgentStreamEvent(event_type=AgentEventType.DELTA, content="got-file")

    monkeypatch.setattr("csagent.api.router_chat.MessageGateway", _GW)
    return TestClient(app), tmp_path


def test_upload_registers_an_artifact(api_client, tmp_path):
    """上传要落进产物清单（M1c），否则事后无从回答「上个月传的那份还在吗」。

    走真实 HTTP 端点而不是直接调 store：这里验的是**接线**，
    store 自身的语义见 test_artifact_store.py。
    """
    from csagent.memory.artifact_store import ArtifactStore, set_artifact_store

    client, _ = api_client
    store = ArtifactStore(tmp_path / "artifacts.db")
    set_artifact_store(store)
    try:
        client.post(
            "/api/files/upload",
            files={"file": ("季度报表.xlsx", b"binary-body", "application/octet-stream")},
        )
        found = store.find(kind="inbound")
    finally:
        set_artifact_store(None)

    assert [r.filename for r in found] == ["季度报表.xlsx"]
    assert found[0].session_id == "web:sess-http"
    assert found[0].size == len(b"binary-body")


def test_upload_survives_a_broken_artifact_ledger(api_client, tmp_path):
    """清单写不进去时，上传本身必须照常成功——它只是一份旁路记录。"""
    from csagent.memory import artifact_store as mod

    class _Broken:
        def register(self, **kwargs):
            raise RuntimeError("ledger is down")

    client, _ = api_client
    mod.set_artifact_store(_Broken())
    try:
        resp = client.post(
            "/api/files/upload",
            files={"file": ("hello.txt", b"hello-upload", "text/plain")},
        )
    finally:
        mod.set_artifact_store(None)

    assert resp.status_code == 200


def test_upload_and_download(api_client):
    client, tmp_path = api_client
    resp = client.post(
        "/api/files/upload",
        files={"file": ("hello.txt", b"hello-upload", "text/plain")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "hello.txt"
    assert data["workspace_path"].endswith("hello.txt")
    assert data["size_bytes"] == len(b"hello-upload")

    assert "inbound" in data["workspace_path"]

    # 文件实际落在 monkeypatch 的 uploads 根下 inbound/
    # session_id = web:sess-http，safe_session_segment 把 ":" 换成 "_"
    disk = tmp_path / "uploads" / "web_sess-http" / "inbound" / "hello.txt"
    assert disk.is_file()

    # download 用 PROJECT_ROOT 相对路径；upload 写到 uploads/web_sess-http/inbound，
    # workspace_relative 相对 PROJECT_ROOT=tmp_path
    dl = client.get("/api/files/download", params={"path": data["workspace_path"]})
    assert dl.status_code == 200
    assert dl.content == b"hello-upload"


def test_build_download_url_relative_by_default(monkeypatch: pytest.MonkeyPatch):
    """无 PUBLIC_BASE_URL 时必须是**不带前导斜杠**的相对路径。

    线上 nginx 只转发 /csagent，聊天页在 /csagent/ 下；写死 /api/... 会打到
    宿主域名根路径而不是本服务。相对路径才跟着页面 URL 走。
    """
    monkeypatch.setattr("csagent.api.router_files._public_base_url", lambda: "")
    url = build_download_url("data/uploads/s/a.txt", session_id="s")
    assert url.startswith("api/files/download?"), url
    assert not url.startswith("/"), "前导斜杠会让链接在 /csagent/ 下失效"
    assert "path=" in url
    # session_id 不再进 URL：它既不参与鉴权，也不该泄进代理日志
    assert "session_id" not in url


def test_build_download_url_with_public_base(monkeypatch: pytest.MonkeyPatch):
    """配了 PUBLIC_BASE_URL 就走绝对地址（企微聊天里的链接必须绝对）。

    线上这个值要连前缀一起写：https://lab.leadfund.com.cn/csagent
    """
    monkeypatch.setattr(
        "csagent.api.router_files._public_base_url",
        lambda: "https://demo.vicp.fun/csagent",
    )
    url = build_download_url("data/uploads/s/a.txt")
    assert url.startswith("https://demo.vicp.fun/csagent/api/files/download?")
    assert "//api" not in url, "拼接时多了一道斜杠"


def test_attachments_from_workspace_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("csagent.api.attachments.PROJECT_ROOT", tmp_path)
    f = tmp_path / "data" / "uploads" / "s" / "a.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x89PNG\r\n")
    atts = attachments_from_workspace_paths(["data/uploads/s/a.png", "missing.txt"])
    assert len(atts) == 1
    assert atts[0].type == AttachmentType.IMAGE


def test_chat_stream_accepts_attachment_paths(api_client):
    client, tmp_path = api_client
    up = client.post(
        "/api/files/upload",
        files={"file": ("note.md", b"# hi", "text/markdown")},
    )
    path = up.json()["workspace_path"]

    # chat 侧 attachments_from_workspace_paths 用 PROJECT_ROOT=tmp_path
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "message": "看看附件",
            "attachment_paths": [path],
        },
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "got-file" in body or "delta" in body
