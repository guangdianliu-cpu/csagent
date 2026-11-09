"""关键词联想：名称索引的匹配规则，以及 /api/chat/suggest。"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.admin import name_index as ni
from csagent.admin import quick_entries as qe
from csagent.api.router_chat import router
from csagent.infra.auth import Principal, require_principal

REPO_CONFIG = Path(__file__).resolve().parents[1] / "configs/profiles/research" / ni.CONFIG_FILE


def _index(db: Path) -> ni.NameIndex:
    """用仓库里的 research 配置，只把库换成临时文件。"""
    raw = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    return ni.NameIndex(dataclasses.replace(ni.NameSources.from_dict(raw), db_path=db))


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE active_equity_funds ("基金代码" TEXT, "基金简称" TEXT, "基金公司" TEXT);
        INSERT INTO active_equity_funds VALUES
            ('001933.OF', '易方达策略成长混合', '易方达'),
            ('110011.OF', '易方达优质精选混合', '易方达');
        CREATE TABLE fund_managers_equity ("基金经理" TEXT, "基金公司" TEXT);
        INSERT INTO fund_managers_equity VALUES ('徐彦', '大成基金'), ('张坤', '易方达');
        CREATE TABLE fund_companies ("基金管理人" TEXT, "基金管理人简称" TEXT);
        INSERT INTO fund_companies VALUES ('易方达基金管理有限公司', '易方达');
        CREATE TABLE private_funds ("产品代码" TEXT, "产品简称" TEXT, "管理人名称" TEXT);
        INSERT INTO private_funds VALUES ('S1', '东方港湾马拉松1号', '东方港湾');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def index(tmp_path: Path) -> ni.NameIndex:
    db = tmp_path / "fund.sqlite3"
    _make_db(db)
    return _index(db)


class TestMatch:
    def test_full_fund_name_beats_company(self, index):
        hit = index.match("易方达策略成长混合近一年怎么样")
        assert hit == ni.NameMatch("fund", "易方达策略成长混合")

    def test_company_alone(self, index):
        assert index.match("易方达") == ni.NameMatch("company", "易方达")

    def test_prefix_while_typing(self, index):
        assert index.match("易方达策略") == ni.NameMatch("fund", "易方达策略成长混合")

    def test_fund_code(self, index):
        assert index.match("110011 的持仓") == ni.NameMatch("fund", "易方达优质精选混合")

    def test_manager_and_private(self, index):
        assert index.match("徐彦") == ni.NameMatch("manager", "徐彦")
        assert index.match("东方港湾") == ni.NameMatch("private", "东方港湾")

    def test_allowed_filter(self, index):
        assert index.match("易方达策略成长混合", {"manager"}) is None

    def test_too_short_or_unknown(self, index):
        assert index.match("易") is None
        assert index.match("不存在的名字") is None

    def test_missing_db_is_empty(self, tmp_path):
        assert _index(tmp_path / "nope.sqlite3").match("易方达") is None

    def test_no_config_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ni, "profile_dir", lambda profile=None: tmp_path)
        assert ni.load_sources() is None
        assert ni.NameIndex(None).match("易方达") is None

    def test_reloads_when_db_replaced(self, tmp_path):
        db = tmp_path / "fund.sqlite3"
        idx = _index(db)
        assert idx.match("徐彦") is None
        _make_db(db)
        # 保证 mtime 与「不存在」时不同，模拟管理后台发布新库
        os.utime(db, (1_000_000, 1_000_000))
        assert idx.match("徐彦") == ni.NameMatch("manager", "徐彦")


ENTRIES = {
    "categories": [
        {
            "key": "public_fund",
            "label": "公募基金",
            "entity": "fund",
            "tags": [
                {"label": "业绩", "questions": ["XXX近一年业绩？", "科技基金有哪些？", "XXX最大回撤？"]},
                {"label": "行业", "questions": ["XXX近一年业绩？", "XXX行业配置？"]},
            ],
        },
        {
            "key": "manager",
            "label": "基金经理",
            "entity": "manager",
            "tags": [{"label": "择时", "questions": ["哪些经理择时好？"]}],
        },
    ]
}


def test_placeholder_questions_dedup_in_order():
    entries = qe.QuickEntries.model_validate(ENTRIES)
    assert qe.placeholder_questions(entries, "fund") == ["XXX近一年业绩？", "XXX最大回撤？", "XXX行业配置？"]
    # 经理分类没有 XXX 问题，不参与联想
    assert qe.suggest_entities(entries) == {"fund"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: ni.NameIndex) -> TestClient:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / qe.DEFAULT_FILE).write_text(yaml.safe_dump(ENTRIES, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(qe, "QUICK_ENTRIES_DIR", tmp_path / "online")
    monkeypatch.setattr(qe, "profile_dir", lambda profile_name=None: profile)
    monkeypatch.setattr("csagent.api.router_chat.get_name_index", lambda: index)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_principal] = lambda: Principal(user_id="u1")
    return TestClient(app)


def test_suggest_route(client):
    data = client.get("/api/chat/suggest", params={"q": "易方达策略成长混合"}).json()
    assert data["entity"] == "fund"
    assert data["suggestions"] == [
        "易方达策略成长混合近一年业绩？",
        "易方达策略成长混合最大回撤？",
        "易方达策略成长混合行业配置？",
    ]


def test_suggest_route_ignores_types_without_templates(client):
    # 认得出徐彦，但经理分类没有 XXX 问题
    assert client.get("/api/chat/suggest", params={"q": "徐彦"}).json()["suggestions"] == []
