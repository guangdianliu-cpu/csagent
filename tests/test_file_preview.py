"""表格文件预览：按显示格式转文字、截断、编码兜底。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import Workbook

from csagent.api import file_preview
from csagent.api.file_preview import sheet_preview


def test_xlsx_formats_and_trims(tmp_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "表1"
    ws.append(["名称", "收益", "规模", "日期", None])
    ws.append(["A", -0.032, 1234567.891, dt.datetime(2026, 10, 28), None])
    ws["B2"].number_format = "0.00%"
    ws["C2"].number_format = "#,##0.00"
    ws.append([None, None, None, None, None])
    wb.create_sheet("说明").append(["数据截至", "2026-10-28"])
    path = tmp_path / "a.xlsx"
    wb.save(path)

    sheets = sheet_preview(path)
    assert [s["name"] for s in sheets] == ["表1", "说明"]
    assert sheets[0]["rows"] == [["名称", "收益", "规模", "日期"], ["A", "-3.20%", "1,234,567.89", "2026-10-28"]]
    assert sheets[0]["truncated"] is False


def test_csv_gbk_and_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_preview, "MAX_ROWS", 2)
    path = tmp_path / "b.csv"
    path.write_bytes("名称,收益\n甲,1\n乙,2\n".encode("gbk"))
    [sheet] = sheet_preview(path)
    assert sheet["rows"] == [["名称", "收益"], ["甲", "1"]]
    assert sheet["total_rows"] == 3 and sheet["truncated"] is True


def test_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "c.xls"
    path.write_bytes(b"x")
    with pytest.raises(ValueError):
        sheet_preview(path)
