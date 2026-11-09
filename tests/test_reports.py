"""报告卡片：积木校验、按会话存版本、publish / update / read 三个工具。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from csagent.channels import workspace_uploads as wu
from csagent.channels.outbound_reports import (
    clear_outbound_reports,
    drain_outbound_reports,
    peek_outbound_reports,
)
from csagent.react.models import CustomerScope
from csagent.reports import store
from csagent.reports.model import ReportContent, readable_errors
from csagent.tools.packs import report_pack

SID = "web:u1"
SCOPE = CustomerScope(session_id=SID, channel="http_api")


@pytest.fixture(autouse=True)
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(wu, "DEFAULT_UPLOADS_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(wu, "PROJECT_ROOT", tmp_path)
    clear_outbound_reports()
    yield tmp_path
    clear_outbound_reports()


def _table(**over) -> dict:
    block = {
        "type": "table",
        "columns": ["指标", "A", "B"],
        "rows": [["近一年", "-3.2%", "1.5%"], ["最大回撤", -12.1, None]],
        "signed_columns": ["A", "B"],
    }
    block.update(over)
    return block


def _content(blocks: list | None = None, **over) -> dict:
    raw = {
        "title": "A 与 B 对比",
        "summary": "A 回撤控制更好",
        "as_of": "2026-10-28",
        "key_points": ["A 回撤更小", "B 收益更高"],
        "sections": [
            {
                "title": "核心指标",
                "blocks": blocks
                if blocks is not None
                else [
                    {"type": "markdown", "text": "## 基本信息\n- 一"},
                    _table(),
                    {"type": "chart", "chart": "line", "x": ["1月", "2月"], "series": [{"name": "A", "data": [1.0, 1.02]}]},
                    {"type": "conclusion", "text": "结论"},
                ],
            }
        ],
        "sources": ["测试库 2026-10-28"],
    }
    raw.update(over)
    return raw


def _run(tool, args: dict) -> str:
    return asyncio.run(tool.execute(args, SCOPE))


class TestModel:
    def test_valid(self):
        content = ReportContent.model_validate(_content())
        assert [b.type for b in content.all_blocks()] == ["markdown", "table", "chart", "conclusion"]

    def test_legacy_flat_blocks_still_load(self):
        # M7/M8 存下的旧报告：平铺 blocks、没有章节
        legacy = {"title": "旧", "summary": "旧", "blocks": [{"type": "markdown", "text": "x"}]}
        content = ReportContent.model_validate(legacy)
        [section] = content.body_sections()
        assert section.title == "" and len(section.blocks) == 1

    def test_no_body_rejected(self):
        with pytest.raises(ValidationError, match="sections 不能为空"):
            ReportContent.model_validate({"title": "t", "summary": "s"})

    @pytest.mark.parametrize(
        "block,needle",
        [
            (_table(rows=[["只有一格"]]), "表头是 3 列"),
            (_table(signed_columns=["C"]), "不在表头里"),
            ({"type": "chart", "chart": "line", "x": ["1月"], "series": [{"name": "A", "data": [1, 2]}]}, "x 有 1 个"),
            ({"type": "chart", "chart": "pie", "x": ["a"], "series": [{"name": "A", "data": [1]}, {"name": "B", "data": [2]}]}, "饼图"),
            ({"type": "html", "text": "<b>"}, "html"),
        ],
    )
    def test_invalid_blocks(self, block, needle):
        with pytest.raises(ValidationError) as exc:
            ReportContent.model_validate(_content(blocks=[block]))
        assert needle in readable_errors(exc.value)

    def test_empty_blocks_rejected(self):
        with pytest.raises(ValidationError):
            ReportContent.model_validate(_content(blocks=[]))


class TestStore:
    def test_versions_and_latest(self):
        content = ReportContent.model_validate(_content())
        r1, path = store.save(SID, content)
        assert r1.version == 1
        assert path.name == "v1.json"
        assert "outbound/reports" in path.as_posix()
        r2, _ = store.save(SID, content, report_id=r1.report_id)
        assert r2.version == 2
        assert store.versions(SID, r1.report_id) == [1, 2]
        assert store.load(SID, r1.report_id).version == 2
        assert store.load(SID, r1.report_id, 1).version == 1
        assert store.load(SID, r1.report_id, 3) is None
        assert store.latest_id(SID) == r1.report_id

    def test_other_session_cannot_see(self):
        r, _ = store.save(SID, ReportContent.model_validate(_content()))
        assert store.load("web:other", r.report_id) is None

    @pytest.mark.parametrize("bad", ["../x", "rpt_../../etc", "rpt_XYZ", ""])
    def test_bad_ids(self, bad):
        assert store.load(SID, bad) is None
        with pytest.raises(ValueError):
            store.save(SID, ReportContent.model_validate(_content()), report_id=bad or "x")


class TestTools:
    def test_publish_update_read(self):
        pub = report_pack.PublishReportTool()
        out = _run(pub, _content())
        assert out.startswith("已发布报告 rpt_")
        [card] = peek_outbound_reports(SID)
        assert card.version == 1
        rid = card.report_id

        # 只改摘要：其余沿用上一版；report_id 不填就是最近一份
        out = _run(report_pack.UpdateReportTool(), {"summary": "改过的摘要"})
        assert f"{rid} v2" in out
        # 同一份报告本轮改两次，队列里只留最后一版
        assert [(c.report_id, c.version) for c in drain_outbound_reports(SID)] == [(rid, 2)]

        data = json.loads(_run(report_pack.ReadReportTool(), {}))
        assert data["summary"] == "改过的摘要"
        assert data["version"] == 2
        assert len(data["sections"][0]["blocks"]) == 4
        assert data["key_points"] == ["A 回撤更小", "B 收益更高"]
        v1 = json.loads(_run(report_pack.ReadReportTool(), {"report_id": rid, "version": 1}))
        assert v1["summary"] == "A 回撤控制更好"

    def test_publish_invalid_returns_error_and_saves_nothing(self):
        out = _run(report_pack.PublishReportTool(), _content(blocks=[_table(rows=[["x"]])]))
        assert out.startswith("错误: 报告格式不对")
        assert "表头是 3 列" in out
        assert peek_outbound_reports(SID) == []
        assert store.latest_id(SID) == ""

    def test_nulls_from_model_are_ignored(self):
        # 模型常把每块的全部字段都带上，不用的填 null
        block = {"type": "markdown", "text": "正文", "columns": None, "rows": None, "chart": None, "title": ""}
        assert _run(report_pack.PublishReportTool(), _content(blocks=[block])).startswith("已发布")

    def test_publish_requires_key_points_and_sections(self):
        out = _run(report_pack.PublishReportTool(), _content(key_points=[]))
        assert out.startswith("错误: 缺少 key_points")
        legacy = {"title": "t", "summary": "s", "blocks": [{"type": "markdown", "text": "x"}]}
        assert "缺少 key_points、sections" in _run(report_pack.PublishReportTool(), legacy)

    def test_update_legacy_report_to_sections(self):
        legacy = ReportContent.model_validate(
            {"title": "旧", "summary": "旧", "blocks": [{"type": "markdown", "text": "x"}]}
        )
        old, _ = store.save(SID, legacy)
        new = _content()
        out = _run(report_pack.UpdateReportTool(), {"report_id": old.report_id, "sections": new["sections"]})
        assert "v2" in out
        latest = store.load(SID, old.report_id)
        assert latest.blocks == [] and latest.sections[0].title == "核心指标"

    def test_update_without_report(self):
        assert "还没有报告" in _run(report_pack.UpdateReportTool(), {"summary": "x"})
        assert "没有报告 rpt_0000abcd" in _run(report_pack.UpdateReportTool(), {"report_id": "rpt_0000abcd"})

    def test_pack(self):
        pack = report_pack.ReportToolPack()
        pack.initialize({})
        assert [t.name for t in pack.get_tools()] == ["publish_report", "update_report", "read_report"]


def test_history_splits_reports_from_files(monkeypatch):
    """报告和文件存在同一列，/api/chat/history 按 kind 拆成 files 与 reports。"""
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from csagent.api.router_chat import router
    from csagent.infra.auth import Principal, require_principal

    transcript = MagicMock()
    transcript.load_recent_with_files.return_value = [
        {"role": "user", "content": "对比一下", "files": []},
        {
            "role": "assistant",
            "content": "结论\n\n[报告] 对比 v2",
            "files": [
                {"filename": "a.xlsx", "workspace_path": "data/uploads/x/outbound/a.xlsx"},
                {"kind": "report", "report_id": "rpt_0000abcd", "version": 2, "title": "对比", "summary": "摘要"},
            ],
        },
    ]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_principal] = lambda: Principal(user_id="u1")
    app.state.transcript_store = transcript
    app.state.main_agent = MagicMock()
    monkeypatch.setattr("csagent.api.router_chat.build_download_url", lambda p, **k: "/dl")
    msgs = TestClient(app).get("/api/chat/history").json()["messages"]
    assert [f["filename"] for f in msgs[1]["files"]] == ["a.xlsx"]
    assert msgs[1]["reports"] == [
        {"report_id": "rpt_0000abcd", "version": 2, "title": "对比", "summary": "摘要", "has_tables": True}
    ]
    assert msgs[0]["reports"] == []


def test_publish_rejects_long_head_fields() -> None:
    """报告头是 5 秒看完的：verdict / 观点 / 指标卡注释 / 章节结论写长了退回重写。"""
    args = _content(verdict="长" * 41, key_points=[{"text": "点" * 61, "tone": "plus"}])
    out = _run(report_pack.PublishReportTool(), args)
    assert out.startswith("错误: 有几处写得太长") and "verdict 41 字" in out and "第 1 条 61 字" in out
    assert _run(report_pack.PublishReportTool(), _content(verdict="短结论")).startswith("已发布")


def test_publish_rejects_long_paragraphs() -> None:
    """正文段落 >160 字退回：读者在手机上看，大段文字没人读。"""
    args = _content([{"type": "markdown", "text": "**论点。** " + "长" * 170}])
    out = _run(report_pack.PublishReportTool(), args)
    assert out.startswith("错误: 有几处写得太长") and "1 段超过 160 字" in out
    ok = _content([{"type": "markdown", "text": "**论点。** 短短的展开。\n\n- 一条\n- 两条"}])
    assert _run(report_pack.PublishReportTool(), ok).startswith("已发布")


def test_publish_accepts_entities_with_null_fields() -> None:
    block = {
        "type": "entities",
        "items": [{"name": "甲", "kind": None, "tags": None}, {"name": "乙", "note": ""}],
        "metrics": [{"label": "收益", "values": [1, 2], "best": "max"}],
    }
    assert _run(report_pack.PublishReportTool(), _content([block])).startswith("已发布")
