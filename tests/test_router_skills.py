"""test_router_skills.py — Skills REST API 端点测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.api.router_admin import require_admin
from csagent.api.router_skills import router
from csagent.infra.auth import Principal, require_principal
from csagent.skills.discovery import SkillDiscovery


def _write_skill(root: Path, name: str, description: str, body: str = "Body content.") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: {description}"
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8"
    )


def _create_app(discovery: SkillDiscovery) -> FastAPI:
    """创建带 skills 路由和 mock state 的测试 app。"""
    app = FastAPI()
    app.include_router(router)
    app.state.skill_discovery = discovery
    # 覆盖鉴权依赖：本文件测 Skill 端点行为，鉴权契约见 test_api_auth.py
    app.dependency_overrides[require_principal] = lambda: Principal(user_id="tester")
    # 热重载只认管理员
    app.dependency_overrides[require_admin] = lambda: Principal(user_id="ops")
    return app


# ---------- GET /api/skills ----------

class TestListSkills:
    def test_list_returns_all(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(root, "alpha", "Alpha Skill 的描述足够长用于通过校验。")
        _write_skill(root, "beta", "Beta Skill 的描述足够长用于通过校验信息。")

        discovery = SkillDiscovery(roots=[root])
        app = _create_app(discovery)
        client = TestClient(app)

        resp = client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        names = [s["name"] for s in data["skills"]]
        assert "alpha" in names
        assert "beta" in names

    def test_list_empty(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        discovery = SkillDiscovery(roots=[root])
        app = _create_app(discovery)
        client = TestClient(app)

        resp = client.get("/api/skills")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_503_without_discovery(self):
        app = FastAPI()
        app.include_router(router)
        # 不设置 skill_discovery；鉴权照常覆盖，否则先被 401 挡住看不到 503
        app.dependency_overrides[require_principal] = lambda: Principal(user_id="tester")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills")
        assert resp.status_code == 503


# ---------- GET /api/skills/{name} ----------

class TestGetSkill:
    def test_get_existing(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(root, "my-skill", "一个描述足够长的 Skill 用于详情接口测试。", body="# Guide")

        discovery = SkillDiscovery(roots=[root])
        app = _create_app(discovery)
        client = TestClient(app)

        resp = client.get("/api/skills/my-skill")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-skill"
        assert data["body_chars"] > 0
        assert data["enabled"] is True

    def test_get_not_found(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        discovery = SkillDiscovery(roots=[root])
        app = _create_app(discovery)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/skills/nonexistent")
        assert resp.status_code == 404


# ---------- POST /api/skills/reload ----------

class TestReloadSkills:
    def test_reload_picks_up_new_skill(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(root, "first", "第一个 Skill 的描述信息足够长用于校验。")

        discovery = SkillDiscovery(roots=[root])
        app = _create_app(discovery)
        client = TestClient(app)

        # 初始只有 1 个
        resp = client.get("/api/skills")
        assert resp.json()["count"] == 1

        # 动态添加第二个
        _write_skill(root, "second", "第二个 Skill 的描述信息也足够长了。")

        # 热重载
        resp = client.post("/api/skills/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reloaded"
        assert data["count"] == 2
        assert "second" in data["skills"]
