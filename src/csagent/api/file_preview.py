"""表格文件（xlsx / csv）转成预览用的二维文本，给对话里的文件卡片「预览」用。

只取前几百行、几十列：预览是看个大概，完整内容走下载。单元格按 Excel 里的
显示格式转文字（百分比、日期、千分位），前端不再猜格式。
PDF 不走这里，前端用 pdf.js 直接画。
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

from openpyxl import load_workbook

MAX_ROWS = 200
MAX_COLS = 30
MAX_SHEETS = 10

SHEET_SUFFIXES = (".xlsx", ".xlsm", ".csv")

_DECIMALS_RE = re.compile(r"0\.(0+)")


def _cell_text(value: object, number_format: str = "General") -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d") if value.time() == dt.time() else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        fmt = number_format or "General"
        m = _DECIMALS_RE.search(fmt)
        decimals = len(m.group(1)) if m else None
        if "%" in fmt:
            return f"{value * 100:.{2 if decimals is None else decimals}f}%"
        if decimals is not None:
            return f"{value:,.{decimals}f}" if "," in fmt else f"{value:.{decimals}f}"
        if isinstance(value, float):
            s = f"{value:.6f}".rstrip("0").rstrip(".")
            return "0" if s in ("-0", "") else s
        return str(value)
    return str(value)


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """去掉尾部空行、每行尾部空列，列数对齐到最宽那行。"""
    while rows and not any(rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows), default=0)
    return [(r + [""] * width)[:width] for r in rows]


def _xlsx_sheets(path: Path) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets[:MAX_SHEETS]:
            rows: list[list[str]] = []
            total = 0
            for row in ws.iter_rows(max_col=MAX_COLS):
                total += 1
                if total <= MAX_ROWS:
                    rows.append([_cell_text(c.value, getattr(c, "number_format", "General")) for c in row])
            rows = _trim(rows)
            sheets.append({"name": ws.title, "rows": rows, "total_rows": total, "truncated": total > MAX_ROWS})
        return sheets
    finally:
        wb.close()


def _csv_sheets(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    rows: list[list[str]] = []
    total = 0
    for row in csv.reader(text.splitlines()):
        total += 1
        if total <= MAX_ROWS:
            rows.append([c.strip() for c in row[:MAX_COLS]])
    return [{"name": path.stem, "rows": _trim(rows), "total_rows": total, "truncated": total > MAX_ROWS}]


def sheet_preview(path: Path) -> list[dict]:
    """返回 ``[{name, rows, total_rows, truncated}]``；不支持的格式抛 ValueError。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _csv_sheets(path)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_sheets(path)
    raise ValueError("该格式暂不支持预览，请下载后查看")
