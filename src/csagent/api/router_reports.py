"""报告读取与导出（docs/对话页升级方案.md §3.3）。

- ``GET /api/reports/{report_id}?v=n``：一版报告 + 已有版本号 + 图表 SVG
- ``GET /api/reports/{report_id}/export?fmt=pdf|xlsx&v=n``：生成导出文件，返回签名下载链接

只按调用者自己的会话去找：会话目录就是权限边界，别人的 report_id 拿到了也是 404。
分享在 M9。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from csagent.api.router_chat import _require_http_channel
from csagent.api.router_files import build_download_url
from csagent.api.schemas import ReportExportResponse, ReportVersionResponse
from csagent.infra.auth import Principal, require_principal
from csagent.reports import export, store
from csagent.reports.model import Report
from csagent.reports.render import best_cells, chart_svg, tally
from csagent.tools.packs.html_pdf_render import HtmlPdfError

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _load(principal: Principal, report_id: str, v: int | None) -> Report:
    if not store.valid_report_id(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    report = store.load(principal.session_id, report_id, v)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在或已过期")
    return report


@router.get("/{report_id}", response_model=ReportVersionResponse)
async def get_report(
    request: Request,
    report_id: str,
    v: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_principal),
) -> ReportVersionResponse:
    _require_http_channel(request)
    report = _load(principal, report_id, v)
    blocks = list(enumerate(report.all_blocks()))
    scored = [(i, b) for i, b in blocks if b.type in ("table", "entities")]
    return ReportVersionResponse(
        report=report.model_dump(),
        versions=store.versions(principal.session_id, report_id),
        charts={str(i): chart_svg(b) for i, b in blocks if b.type == "chart"},
        highlights={str(i): cells for i, b in scored if (cells := best_cells(b))},
        tallies={str(i): t for i, b in scored if (t := tally(b))},
        risk_note_default=export.risk_note_default(),
    )


@router.get("/{report_id}/export", response_model=ReportExportResponse)
async def export_report(
    request: Request,
    report_id: str,
    fmt: str = Query(..., pattern="^(pdf|xlsx)$"),
    v: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_principal),
) -> ReportExportResponse:
    _require_http_channel(request)
    report = _load(principal, report_id, v)
    try:
        path = await export.export(principal.session_id, report, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HtmlPdfError as exc:
        raise HTTPException(status_code=503, detail=f"PDF 暂时生成不了：{exc}") from exc
    return ReportExportResponse(
        filename=path.name,
        url=build_download_url(store.relative_path(path), session_id=principal.session_id),
        size=path.stat().st_size,
    )
