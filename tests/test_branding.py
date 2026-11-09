"""悬浮图标：管理端上传/切换/恢复，公开端读取（免鉴权 + CORS + SVG 沙箱）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.admin import branding
from csagent.admin.passwd import hash_password
from csagent.api import router_admin, router_branding

SECRET = "test-secret-that-is-long-enough-for-hmac-32"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = MagicMock()
    settings.agent_auth_enabled = True
    settings.agent_shared_secret = SECRET
    settings.admin_accounts = f"ops:{hash_password('pass-12345')}"
    settings.admin_token_ttl_seconds = 600
    settings.admin_token_secret = "admin-only-secret-never-shared-with-the-host"
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_admin.get_settings", lambda: settings)
    monkeypatch.setattr(branding, "BRANDING_DIR", tmp_path / "branding")
    router_admin._failures.clear()
    app = FastAPI()
    app.include_router(router_admin.router)
    app.include_router(router_branding.router)
    c = TestClient(app)
    token = c.post("/api/admin/login", json={"username": "ops", "password": "pass-12345"}).json()["token"]
    c.admin = {"Authorization": f"Bearer {token}"}  # type: ignore[attr-defined]
    return c


def test_default_is_unconfigured(client):
    data = client.get("/api/branding").json()
    assert data["icon_url"] is None
    assert client.get("/api/branding/icon").status_code == 404


def test_public_endpoint_allows_cross_origin(client):
    resp = client.get("/api/branding")
    assert resp.headers["access-control-allow-origin"] == "*"


def test_upload_requires_admin(client):
    resp = client.post("/api/admin/branding/icon", files={"file": ("a.png", PNG, "image/png")})
    assert resp.status_code == 401


def test_upload_png_then_public_serves_it(client):
    resp = client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.png", PNG, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    pub = client.get("/api/branding").json()
    assert pub["icon_url"].startswith("api/branding/icon?v=")
    icon = client.get("/api/branding/icon")
    assert icon.status_code == 200
    assert icon.content == PNG
    assert icon.headers["content-type"] == "image/png"
    assert "sandbox" in icon.headers["content-security-policy"]


def test_svg_served_sandboxed(client):
    client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.svg", SVG, "image/svg+xml")},
    )
    icon = client.get("/api/branding/icon")
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in icon.headers["content-security-policy"]


def test_rejects_fake_or_oversize_image(client):
    fake = client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.png", b"not an image", "image/png")},
    )
    assert fake.status_code == 400
    wrong_ext = client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.gif", b"GIF89a", "image/gif")},
    )
    assert wrong_ext.status_code == 400
    big = client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.png", PNG + b"\x00" * branding.MAX_ICON_BYTES, "image/png")},
    )
    assert big.status_code == 413


def test_mode_switch_and_version_bump(client):
    client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.png", PNG, "image/png")},
    )
    v1 = client.get("/api/branding").json()["version"]
    resp = client.put("/api/admin/branding", headers=client.admin, json={"icon_mode": "fill"})
    assert resp.json()["branding"]["icon_mode"] == "fill"
    pub = client.get("/api/branding").json()
    assert pub["icon_mode"] == "fill"
    assert pub["version"] > v1
    bad = client.put("/api/admin/branding", headers=client.admin, json={"icon_mode": "huge"})
    assert bad.status_code == 400


def test_replacing_icon_keeps_single_file_and_reset(client, tmp_path: Path):
    client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.png", PNG, "image/png")},
    )
    client.post(
        "/api/admin/branding/icon", headers=client.admin,
        files={"file": ("logo.svg", SVG, "image/svg+xml")},
    )
    icons = [p.name for p in (tmp_path / "branding").glob("launcher-icon*")]
    assert icons == ["launcher-icon.svg"]
    resp = client.delete("/api/admin/branding/icon", headers=client.admin)
    assert resp.json()["branding"]["icon_file"] == ""
    assert client.get("/api/branding").json()["icon_url"] is None
    assert not list((tmp_path / "branding").glob("launcher-icon*"))
