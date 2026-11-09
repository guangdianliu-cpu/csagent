"""HTML → PDF 工具：路径约束、默认落点、产物登记、引擎缺失时的人话错误。"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.channels.workspace_uploads import safe_session_segment, session_outbound_rel
from csagent.memory.artifact_store import ArtifactStore, set_artifact_store
from csagent.react.models import CustomerScope
from csagent.tools.packs.html_pdf_render import (
    HtmlPdfError,
    _inject_style,
    available_engines,
    resolve_engine,
)
from csagent.tools.packs.pdf_pack import HtmlPdfToolPack, HtmlToPdfTool


SESSION = "web:u1"
SCOPE = CustomerScope(session_id=SESSION, channel="http_api")


@pytest.fixture
def workspace(tmp_path):
    """工具根 = 临时目录，避免写进仓库 data/。"""
    store = ArtifactStore(tmp_path / "csagent.db")
    set_artifact_store(store)
    yield tmp_path
    set_artifact_store(None)


def _html_file(root: Path, name: str = "doc.html", *, inbound: bool = False) -> Path:
    from csagent.channels.workspace_uploads import session_inbound_rel

    rel = Path(session_inbound_rel(SESSION) if inbound else session_outbound_rel(SESSION)) / name
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<html><head></head><body><p>hello</p></body></html>", encoding="utf-8")
    return path


def _tool(root: Path, **kwargs) -> HtmlToPdfTool:
    return HtmlToPdfTool(root, session_scoped=True, **kwargs)


async def _fake_render(html_path: Path, pdf_path: Path, **kwargs) -> str:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    return "weasyprint"


@pytest.mark.asyncio
async def test_writes_pdf_next_to_default_outbound_and_registers(workspace, monkeypatch):
    monkeypatch.setattr(
        "csagent.tools.packs.pdf_pack.render_html_to_pdf_async", _fake_render
    )
    _html_file(workspace)
    tool = _tool(workspace)
    out = await tool.execute({"html_path": "doc.html"}, SCOPE)

    pdf = workspace / session_outbound_rel(SESSION) / "doc.pdf"
    assert pdf.is_file()
    assert "已生成 PDF" in out
    assert "doc.pdf" in out
    assert "send_file" in out
    records = ArtifactStore(workspace / "csagent.db").find(
        session_id=SESSION, kind="outbound"
    )
    assert any(r.filename == "doc.pdf" and r.kind == "outbound" for r in records)


@pytest.mark.asyncio
async def test_custom_pdf_path_is_session_bound(workspace, monkeypatch):
    monkeypatch.setattr(
        "csagent.tools.packs.pdf_pack.render_html_to_pdf_async", _fake_render
    )
    _html_file(workspace)
    tool = _tool(workspace)
    out = await tool.execute(
        {"html_path": "doc.html", "pdf_path": "report.pdf"}, SCOPE
    )
    expected = workspace / session_outbound_rel(SESSION) / "report.pdf"
    assert expected.is_file()
    assert "report.pdf" in out


@pytest.mark.asyncio
async def test_inbound_html_still_writes_pdf_to_outbound(workspace, monkeypatch):
    """用户上传的 HTML 在 inbound，产物必须落到 outbound，不能改写上传目录。"""
    monkeypatch.setattr(
        "csagent.tools.packs.pdf_pack.render_html_to_pdf_async", _fake_render
    )
    _html_file(workspace, inbound=True)
    tool = _tool(workspace)
    out = await tool.execute({"html_path": "doc.html"}, SCOPE)
    outbound_pdf = workspace / session_outbound_rel(SESSION) / "doc.pdf"
    inbound_pdf = workspace / Path(session_outbound_rel(SESSION)).parent / "inbound" / "doc.pdf"
    assert outbound_pdf.is_file()
    assert not inbound_pdf.exists()
    assert "outbound" in out


@pytest.mark.asyncio
async def test_missing_html_returns_error(workspace):
    tool = _tool(workspace)
    out = await tool.execute({"html_path": "nope.html"}, SCOPE)
    assert out.startswith("错误:")
    assert "找不到" in out


@pytest.mark.asyncio
async def test_rejects_non_html_suffix(workspace):
    tool = _tool(workspace)
    out = await tool.execute({"html_path": "notes.txt"}, SCOPE)
    assert "必须是 .html" in out


@pytest.mark.asyncio
async def test_empty_html_path(workspace):
    tool = _tool(workspace)
    out = await tool.execute({}, SCOPE)
    assert "缺少参数 html_path" in out


@pytest.mark.asyncio
async def test_engine_missing_is_plain_language(workspace, monkeypatch):
    async def _boom(*_a, **_k):
        raise HtmlPdfError("没有可用的 PDF 引擎。任选其一：pip install 'cs-agent[pdf]'")

    monkeypatch.setattr(
        "csagent.tools.packs.pdf_pack.render_html_to_pdf_async", _boom
    )
    _html_file(workspace)
    tool = _tool(workspace)
    out = await tool.execute({"html_path": "doc.html"}, SCOPE)
    assert out.startswith("错误:")
    assert "cs-agent[pdf]" in out
    assert not (workspace / session_outbound_rel(SESSION) / "doc.pdf").exists()


def test_pack_initialize_exposes_html_to_pdf(tmp_path):
    pack = HtmlPdfToolPack()
    pack.initialize({"root_dir": str(tmp_path), "session_scoped": True})
    names = [t.name for t in pack.get_tools()]
    assert names == ["html_to_pdf"]


def test_resolve_engine_rejects_unknown():
    with pytest.raises(HtmlPdfError, match="未知引擎"):
        resolve_engine("ghostscript")


def test_inject_style_into_head():
    html = "<html><head><title>t</title></head><body></body></html>"
    out = _inject_style(html, "body{color:red}")
    assert "<style>\nbody{color:red}</style>" in out
    assert out.index("<style>") < out.index("</head>")


def test_inject_style_without_head_prefixes():
    html = "<p>hi</p>"
    out = _inject_style(html, "p{margin:0}")
    assert out.startswith("<style>")
    assert out.endswith("<p>hi</p>")


def test_capability_notes_mention_html_to_pdf():
    from csagent.prompts.capability_notes import format_capability_notes

    notes = format_capability_notes(["write_file", "html_to_pdf"])
    assert "html_to_pdf" in notes
    assert "write_file" in notes
    assert "版式" in notes


def test_session_segment_is_safe():
    assert ":" not in safe_session_segment(SESSION)


@pytest.mark.skipif(
    not available_engines(),
    reason="本机没有 WeasyPrint 也没有 Chrome/Edge",
)
@pytest.mark.asyncio
async def test_real_engine_writes_pdf_header(workspace):
    """有引擎时走一遍真渲染，确认铺的路在本机是通的。"""
    from csagent.tools.packs.html_pdf_render import render_html_to_pdf_async

    html = _html_file(workspace)
    pdf = html.with_suffix(".pdf")
    engine = await render_html_to_pdf_async(html, pdf)
    assert engine in {"weasyprint", "chromium"}
    assert pdf.is_file()
    assert pdf.read_bytes()[:4] == b"%PDF"
