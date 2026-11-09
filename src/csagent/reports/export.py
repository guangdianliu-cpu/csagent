"""报告导出：PDF（积木 → HTML → 复用 html_pdf_render）与 Excel（每张表一个 sheet）。

导出文件按版本缓存在 ``reports/<id>/exports/v<n>.pdf|xlsx``：一个版本落盘后内容不再变，
同一版点几次导出只渲染一次。下载走现有的签名下载链接（与 send_file 的文件同一条路）。
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from csagent.reports import store
from csagent.reports.model import Report
from csagent.reports.render import report_html, signed_class
from csagent.tools.packs.html_pdf_render import render_html_to_pdf_async

logger = structlog.get_logger(__name__)

FORMATS = ("pdf", "xlsx")
EXPORTS_DIRNAME = "exports"

# ReportToolPack.initialize 写入（tool_packs.yaml report.config）
_pdf_options: dict = {"engine": "auto", "chromium_path": "", "timeout_seconds": 60.0}
# 通用风险提示与 PDF 页脚：行业措辞放形态配置（tool_pack_overrides.report.config）
_texts: dict = {"risk_note": "本报告仅供参考，不构成任何建议。", "footer": ""}


def configure(options: dict | None, texts: dict | None = None) -> None:
    for key in _pdf_options:
        if options and options.get(key) is not None:
            _pdf_options[key] = options[key]
    for key in _texts:
        if texts and texts.get(key) is not None:
            _texts[key] = str(texts[key])


def risk_note_default() -> str:
    return _texts["risk_note"]


def footer_text() -> str:
    return _texts["footer"]


def has_tables(report: Report) -> bool:
    return any(b.type == "table" for b in report.all_blocks())


def export_filename(report: Report, fmt: str) -> str:
    """下载时看到的文件名：标题 + 版本。去掉文件名里不能用的字符。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", report.title).strip("_")[:60] or "报告"
    return f"{safe}_v{report.version}.{fmt}"


def _export_path(session_id: str, report: Report, fmt: str, *, root: Path | None = None) -> Path:
    d = store.reports_dir(session_id, root=root) / report.report_id / EXPORTS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / export_filename(report, fmt)


async def export(session_id: str, report: Report, fmt: str, *, root: Path | None = None) -> Path:
    """生成（或取缓存的）导出文件，返回绝对路径。

    :raises ValueError: 格式不认识，或要 Excel 但报告里没有表格
    :raises HtmlPdfError: PDF 渲染失败（没有可用引擎等）
    """
    if fmt not in FORMATS:
        raise ValueError(f"不支持的导出格式：{fmt}")
    if fmt == "xlsx" and not has_tables(report):
        raise ValueError("这份报告没有表格，无需导出 Excel")
    path = _export_path(session_id, report, fmt, root=root)
    if path.is_file() and path.stat().st_size > 0:
        return path
    if fmt == "pdf":
        html_path = path.with_suffix(".html")
        html_path.write_text(
            report_html(report, risk_note_default=risk_note_default(), footer=footer_text()),
            encoding="utf-8",
        )
        engine = await render_html_to_pdf_async(
            html_path,
            path,
            engine=str(_pdf_options["engine"]),
            chromium_path=str(_pdf_options["chromium_path"]),
            timeout_seconds=float(_pdf_options["timeout_seconds"]),
        )
        html_path.unlink(missing_ok=True)
        logger.info("report_exported", report_id=report.report_id, fmt=fmt, engine=engine)
    else:
        write_xlsx(report, path)
        logger.info("report_exported", report_id=report.report_id, fmt=fmt)
    return path


_PCT_RE = re.compile(r"^\s*([+\-−–＋]?)\s*(\d+(?:\.\d+)?)\s*%\s*$")
_NUM_RE = re.compile(r"^\s*([+\-−–＋]?)\s*(\d+(?:\.\d+)?)\s*$")
_HEAD_FILL = PatternFill("solid", fgColor="F3F3F3")
_UP_FONT = Font(color="E85951")
_DOWN_FONT = Font(color="48C993")


def _cell_value(value: object) -> tuple[object, str]:
    """「-3.2%」这类文字转成真数字，Excel 里才能排序求和。返回 (值, 数字格式)。"""
    if not isinstance(value, str):
        return value, ""
    for regex, scale, fmt in ((_PCT_RE, 100.0, "0.00%"), (_NUM_RE, 1.0, "")):
        m = regex.match(value)
        if m:
            sign = -1.0 if m.group(1) and m.group(1) in "-−–" else 1.0
            return sign * float(m.group(2)) / scale, fmt
    return value, ""


def _sheet_title(title: str, index: int, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", title).strip() or f"表{index}"
    base = base[:28]
    name, n = base, 2
    while name in used:
        name = f"{base[:26]}_{n}"
        n += 1
    used.add(name)
    return name


def write_xlsx(report: Report, path: Path) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    tables = [b for b in report.all_blocks() if b.type == "table"]
    for i, block in enumerate(tables, 1):
        # 与报告里的「表 n」对得上
        ws = wb.create_sheet(_sheet_title(f"表{i} {block.title}".strip(), i, used))
        ws.append(list(block.columns))
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = _HEAD_FILL
        signed = {block.columns.index(c) for c in block.signed_columns}
        widths = [len(str(c)) for c in block.columns]
        for r, row in enumerate(block.rows, 2):
            for j, raw in enumerate(row):
                value, fmt = _cell_value(raw)
                cell = ws.cell(row=r, column=j + 1, value=value)
                if fmt:
                    cell.number_format = fmt
                if j in signed:
                    cls = signed_class(raw)
                    if cls:
                        cell.font = _UP_FONT if cls == "num-up" else _DOWN_FONT
                widths[j] = max(widths[j], len(str(raw if raw is not None else "")))
        for j, w in enumerate(widths, 1):
            # 中文按两个字符宽估
            ws.column_dimensions[get_column_letter(j)].width = min(48, max(8, w * 2 + 2))
        ws.freeze_panes = "A2"
        for row in ws.iter_rows(min_row=1, max_row=1):
            for cell in row:
                cell.alignment = Alignment(vertical="center")
    # 末尾一页说明：标题、摘要、来源，免得表格脱离上下文
    info = wb.create_sheet("说明")
    info.append(["标题", report.title])
    info.append(["摘要", report.summary])
    info.append(["版本", f"v{report.version}"])
    if report.as_of:
        info.append(["数据截至", report.as_of])
    if report.verdict:
        info.append(["结论", report.verdict])
    for kpi in report.kpis:
        info.append([kpi.label, f"{kpi.value}  {kpi.note}".strip()])
    for k, point in enumerate(report.points(), 1):
        info.append([f"核心观点 {k}", point.text])
    for s in report.sources:
        info.append(["数据来源", s])
    info.column_dimensions["A"].width = 12
    info.column_dimensions["B"].width = 80
    tmp = path.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    tmp.replace(path)
