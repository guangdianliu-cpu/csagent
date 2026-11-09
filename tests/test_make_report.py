"""大名单出表：multi_lookup 落盘 → make_report 出 xlsx 的单元测试。"""

from __future__ import annotations

import json

import pytest

from make_report import build_rows, main as report_main
from multi_fund_tool import resolve_out_path, run_multi_fund_lookup


class _StubClient:
    """只实现 basic/limits 两路的最小 client（其余 aspect 本文件用不到）。"""

    def __init__(self, *, details=None):
        self._details = details or {}

    async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
        row = dict(self._details.get(fund_code) or {"fund_code": fund_code})
        row.setdefault("custtype", custtype)
        return row

    async def trade_limits_batch(self, codes):
        return []


@pytest.fixture()
def outbound(tmp_path, monkeypatch):
    d = tmp_path / "outbound"
    d.mkdir()
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(d))
    return d


# ── out_file 落点 ────────────────────────────────────────
def test_resolve_out_path_uses_session_outbound(outbound):
    p = resolve_out_path("funds.json")
    assert p.parent == outbound
    assert p.name == "funds.json"


def test_resolve_out_path_strips_traversal_and_adds_suffix(outbound):
    assert resolve_out_path("../../etc/passwd").name == "passwd.json"
    assert resolve_out_path("a/b/c/funds").name == "funds.json"
    assert resolve_out_path("") is None
    assert resolve_out_path(None) is None


# ── 落盘 + 摘要 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_out_file_writes_full_and_returns_summary(outbound):
    details = {
        f"{i:06d}": {"fund_code": f"{i:06d}", "fund_name": f"测试基金{i}", "consign_flag": "1"}
        for i in range(20)
    }
    raw = await run_multi_fund_lookup(
        _StubClient(details=details), list(details), aspects=["basic"], out_file="funds.json"
    )
    summary = json.loads(raw)

    # stdout 是精简视图 + 落盘信息，不是整份数据
    assert summary["saved_to"] == "funds.json"
    assert summary["count"] == 20
    assert len(raw) < summary["saved_chars"]

    saved = json.loads((outbound / "funds.json").read_text(encoding="utf-8"))
    assert len(saved["funds"]) == 20  # 落盘的是完整视图


@pytest.mark.asyncio
async def test_out_file_failure_is_reported_not_swallowed(outbound, monkeypatch):
    """落盘失败不该把整次查询变成脚本异常——数据已经在手上了。"""
    blocker = outbound / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(blocker / "sub"))

    raw = await run_multi_fund_lookup(
        _StubClient(details={"X": {"fund_code": "X", "fund_name": "测试"}}),
        ["X"], aspects=["basic"], out_file="f.json",
    )
    result = json.loads(raw)
    assert "落盘失败" in result["out_file_error"]
    assert result["count"] == 1          # 结果照样给出来
    assert "saved_to" not in result


# ── 出表 ─────────────────────────────────────────────────
def _payload() -> dict:
    return {
        "custtype": "0",
        "funds": {
            "006406": {
                "basic": [{
                    "fund_code": "006406", "fund_name": "华富恒盛纯债债券C",
                    "fund_type_name": "债券型", "risky_name": "中低风险",
                    "consign_flag": "1", "fundstate": "0",
                    "buystatus": "1", "redeemstatus": "1",
                }],
                "limits": [{"minvalue": 1, "maxvalue": 9999999999}],
            },
            "015523": {
                "basic": [{
                    "fund_code": "015523", "fund_name": "华商鸿盛纯债债券",
                    "consign_flag": "", "buystatus": "0", "fundstate": "5",
                }],
                "limits": [{"minvalue": 1, "maxvalue": 100000}],
            },
        },
    }


def test_build_rows_translates_codes_and_limits():
    rows = build_rows(_payload())
    assert len(rows) == 2
    first, second = rows
    assert first[0] == "华富恒盛纯债债券C"
    assert first[4] == "是"          # consign_flag=1
    assert first[6] == "可申购"       # buystatus=1
    assert first[11] == "暂无明确上限"  # 天文数字不念
    # 空状态一律「未返回」，不能当成「否」
    assert second[4] == "未返回"
    assert second[5] == "停止申购"
    assert second[6] == "已关闭"
    assert second[11] == 100000


def test_build_rows_accepts_compact_view():
    """降级视图 funds 是数组，出表要照样吃得下。"""
    compact = {
        "custtype": "0",
        "view": "compact",
        "funds": [{"fund_code": "006406", "fund_name": "华富恒盛纯债债券C",
                   "consign_flag": "1", "buystatus": "1", "minvalue": 1}],
    }
    rows = build_rows(compact)
    assert rows[0][1] == "006406"
    assert rows[0][4] == "是"


def test_report_main_writes_xlsx(outbound):
    (outbound / "funds.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )
    report_main(["funds.json", "代销核对"])
    dest = outbound / "代销核对.xlsx"
    assert dest.is_file()

    from openpyxl import load_workbook
    ws = load_workbook(dest).active
    assert ws.cell(row=1, column=1).value == "产品名称"
    assert ws.cell(row=2, column=1).value == "华富恒盛纯债债券C"
    assert ws.freeze_panes == "A2"


def test_build_rows_marks_failed_aspects():
    """接口调用失败的格子写「查询失败」，不能混成「未返回」。"""
    rows = build_rows({"funds": {
        "000001": {"failed": ["basic"], "limits": [{"minvalue": 10}]},
        "000002": {"basic": [{"fund_code": "000002", "fund_name": "乙", "consign_flag": ""}]},
    }})
    first, second = rows
    assert first[1] == "000001"          # 代码照常显示
    assert first[0] == "查询失败"         # 名称来自 basic，没查到
    assert first[4] == "查询失败"         # 是否代销
    assert first[10] == 10               # limits 查到了，照常
    assert second[4] == "未返回"          # 查到了但没给状态，仍是「未返回」


def test_report_main_reads_tool_payload_from_stdin(outbound, monkeypatch, capsys):
    """product_make_report 原生工具：{"source", "target"} 经 stdin 传入。"""
    import io

    (outbound / "funds.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO('{"source": "funds.json", "target": "核对"}'))
    assert report_main(["--payload", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["saved_to"] == "核对.xlsx"
    assert (outbound / "核对.xlsx").is_file()


def test_report_main_reports_missing_input(outbound, capsys):
    assert report_main(["nope.json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "BAD_PAYLOAD"
    assert "参数错误" in out["error"]["message"]


def test_report_main_reports_bad_json(outbound, capsys):
    (outbound / "broken.json").write_text('{"funds": {"006406"', encoding="utf-8")
    assert report_main(["broken.json"]) == 1
    assert "不是合法 JSON" in json.loads(capsys.readouterr().out)["error"]["message"]
