"""快捷入口：两层来源的优先级、校验，以及 /api/chat/entries。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from csagent.admin import quick_entries as qe
from csagent.api.router_chat import router
from csagent.infra.auth import Principal, require_principal

REPO_DEFAULT = Path(__file__).resolve().parents[1] / "configs/profiles/research/quick_entries.yaml"


def _content(label: str = "公募基金") -> dict:
    return {
        "welcome": {"title": "Hi", "groups": [{"label": "组", "questions": ["问题一？"]}]},
        "categories": [
            {
                "key": "public_fund",
                "label": label,
                "entity": "fund",
                "tags": [{"label": "QDII基金", "keywords": ["QDII 债券型"], "questions": ["XXX近一年业绩？"]}],
            }
        ],
    }


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(形态目录, 线上目录)，形态固定为 research。"""
    profile = tmp_path / "profiles" / "research"
    profile.mkdir(parents=True)
    online = tmp_path / "data" / "quick_entries"
    monkeypatch.setattr(qe, "QUICK_ENTRIES_DIR", online)
    monkeypatch.setattr(qe, "active_profile", lambda: "research")
    monkeypatch.setattr(qe, "profile_dir", lambda profile_name=None: profile)
    return profile, online


def _write_default(profile: Path, content: dict | str) -> None:
    text = content if isinstance(content, str) else yaml.safe_dump(content, allow_unicode=True)
    (profile / qe.DEFAULT_FILE).write_text(text, encoding="utf-8")


def _write_online(online: Path, content: dict, version: int = 7) -> None:
    online.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "updated_by": "ops", "updated_at": 1, "content": content}
    (online / "research.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestLoad:
    def test_none(self, dirs):
        loaded = qe.load()
        assert loaded.source == "none"
        assert loaded.entries is None

    def test_default_only(self, dirs):
        _write_default(dirs[0], _content())
        loaded = qe.load()
        assert loaded.source == "default"
        assert loaded.entries.categories[0].label == "公募基金"
        assert loaded.version > 0

    def test_online_wins(self, dirs):
        _write_default(dirs[0], _content("默认"))
        _write_online(dirs[1], _content("线上"))
        loaded = qe.load()
        assert loaded.source == "online"
        assert loaded.version == 7
        assert loaded.updated_by == "ops"
        assert loaded.entries.categories[0].label == "线上"

    def test_bad_online_falls_back_to_default(self, dirs):
        _write_default(dirs[0], _content("默认"))
        dirs[1].mkdir(parents=True)
        (dirs[1] / "research.json").write_text("{坏的", encoding="utf-8")
        assert qe.load().source == "default"

    def test_bad_default_hides(self, dirs):
        _write_default(dirs[0], "categories: [不是对象")
        assert qe.load().source == "none"

    def test_repo_default_is_valid(self):
        raw = yaml.safe_load(REPO_DEFAULT.read_text(encoding="utf-8"))
        entries = qe.QuickEntries.model_validate(raw)
        assert [c.label for c in entries.categories] == ["公募基金", "基金经理", "基金公司", "私募基金"]
        assert len(entries.welcome.groups) == 4


class TestValidation:
    def test_two_placeholders_rejected(self):
        bad = _content()
        bad["categories"][0]["tags"][0]["questions"] = ["XXX和XXX比谁好？"]
        with pytest.raises(ValidationError, match="最多 1 个"):
            qe.QuickEntries.model_validate(bad)

    def test_duplicate_key_rejected(self):
        bad = _content()
        bad["categories"].append(bad["categories"][0])
        with pytest.raises(ValidationError, match="重复"):
            qe.QuickEntries.model_validate(bad)

    def test_long_question_rejected(self):
        bad = _content()
        bad["welcome"]["groups"][0]["questions"] = ["长" * 61]
        with pytest.raises(ValidationError, match="60"):
            qe.QuickEntries.model_validate(bad)

    def test_bad_entity_rejected(self):
        bad = _content()
        bad["categories"][0]["entity"] = "stock"
        with pytest.raises(ValidationError):
            qe.QuickEntries.model_validate(bad)


def _client(principal: Principal | None = Principal(user_id="u1")) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if principal is not None:
        app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


class TestRoute:
    def test_returns_entries(self, dirs):
        _write_default(dirs[0], _content())
        data = _client().get("/api/chat/entries").json()
        assert data["source"] == "default"
        assert data["entries"]["categories"][0]["tags"][0]["label"] == "QDII基金"

    def test_none(self, dirs):
        data = _client().get("/api/chat/entries").json()
        assert data == {"source": "none", "version": 0, "entries": None}


class TestSaveAndHistory:
    def test_save_overrides_default_and_bumps_version(self, dirs):
        _write_default(dirs[0], _content("默认"))
        base = qe.load().version
        loaded = qe.save(qe.QuickEntries.model_validate(_content("新")), "ops", base_version=base)
        assert loaded.source == "online"
        assert loaded.version > base
        assert loaded.updated_by == "ops"
        assert loaded.entries.categories[0].label == "新"

    def test_stale_base_version_rejected(self, dirs):
        _write_default(dirs[0], _content())
        with pytest.raises(qe.VersionConflict):
            qe.save(qe.QuickEntries.model_validate(_content()), "ops", base_version=123)

    def test_history_keeps_latest_five_and_rollback(self, dirs):
        _write_online(dirs[1], _content("v0"), version=1)
        for i in range(1, 8):
            current = qe.load()
            qe.save(qe.QuickEntries.model_validate(_content(f"v{i}")), "ops", base_version=current.version)
        hist = qe.history()
        assert len(hist) == qe.HISTORY_KEEP
        assert hist == sorted(hist, key=lambda h: -h["version"])
        # 回到最近一份历史：内容回去，版本号继续往上走
        target = hist[0]["version"]
        before = qe.load().version
        loaded = qe.rollback(target, "ops")
        assert loaded.entries.categories[0].label == "v6"
        assert loaded.version > before

    def test_reset_falls_back_to_default_and_archives(self, dirs):
        _write_default(dirs[0], _content("默认"))
        _write_online(dirs[1], _content("线上"))
        loaded = qe.reset()
        assert loaded.source == "default"
        assert [h["version"] for h in qe.history()] == [7]

    def test_rollback_missing(self, dirs):
        with pytest.raises(FileNotFoundError):
            qe.rollback(42, "ops")


def _admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from csagent.api import router_admin

    monkeypatch.setattr(router_admin, "record_security_event", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(router_admin.router)
    app.dependency_overrides[router_admin.require_admin] = lambda: Principal(user_id="ops")
    return TestClient(app)


class TestAdminRoutes:
    def test_get_save_conflict_rollback_reset(self, dirs, monkeypatch):
        _write_default(dirs[0], _content("默认"))
        client = _admin_client(monkeypatch)

        state = client.get("/api/admin/quick-entries").json()
        assert state["source"] == "default"
        assert state["history"] == []

        resp = client.put(
            "/api/admin/quick-entries",
            json={"base_version": state["version"], "entries": _content("新")},
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["source"] == "online"
        assert saved["entries"]["categories"][0]["label"] == "新"

        # 拿旧版本号再存一次 → 409
        resp = client.put(
            "/api/admin/quick-entries",
            json={"base_version": state["version"], "entries": _content("又一次")},
        )
        assert resp.status_code == 409

        resp = client.delete("/api/admin/quick-entries")
        assert resp.json()["source"] == "default"
        archived = resp.json()["history"][0]["version"]
        assert archived == saved["version"]

        resp = client.post("/api/admin/quick-entries/rollback", json={"version": archived})
        assert resp.json()["entries"]["categories"][0]["label"] == "新"

    def test_validation_error_is_readable(self, dirs, monkeypatch):
        client = _admin_client(monkeypatch)
        bad = _content()
        bad["categories"][0]["tags"][0]["questions"] = ["XXX 和 XXX"]
        resp = client.put("/api/admin/quick-entries", json={"base_version": 0, "entries": bad})
        assert resp.status_code == 400
        assert isinstance(resp.json()["detail"], str)
        assert "最多 1 个" in resp.json()["detail"]
        assert "第 1 个分类 › 第 1 个能力胶囊 › 问题" in resp.json()["detail"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/quick-entries"),
        ("put", "/api/admin/quick-entries"),
        ("post", "/api/admin/quick-entries/rollback"),
        ("delete", "/api/admin/quick-entries"),
    ],
)
def test_admin_routes_require_admin(monkeypatch, method, path):
    from unittest.mock import MagicMock

    from csagent.api import router_admin

    settings = MagicMock()
    settings.agent_auth_enabled = True
    settings.admin_token_secret = "admin-only-secret-never-shared-with-the-host"
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_admin.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router_admin.router)
    resp = getattr(TestClient(app), method)(path)
    assert resp.status_code == 401
