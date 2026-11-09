"""聊天页外壳：index.html 引用的样式与脚本带内容版本号，页面本身不缓存。"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from csagent import main


def _client() -> TestClient:
    # 不进 with：不跑 lifespan，只测静态路由
    return TestClient(main.create_app())


def test_index_fills_asset_version():
    resp = _client().get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    html = resp.text
    assert "__ASSET_V__" not in html
    version = main._chat_asset_version()
    assert re.fullmatch(r"[0-9a-f]{10}", version)
    assert f'assets/chat/app.js?v={version}' in html
    assert f'assets/chat/chat.css?v={version}' in html


def test_chat_assets_served():
    client = _client()
    for name in ("app.js", "chat.css"):
        assert client.get(f"/assets/chat/{name}").status_code == 200


def test_asset_version_follows_content(tmp_path, monkeypatch):
    chat_dir = tmp_path / "assets" / "chat"
    chat_dir.mkdir(parents=True)
    (chat_dir / "app.js").write_text("a", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    before = main._chat_asset_version()
    (chat_dir / "app.js").write_text("b", encoding="utf-8")
    assert main._chat_asset_version() != before
