"""本地文档文本抽取：纯文本 / PDF / Word / Excel（可选依赖）。"""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"}
_MAX_CHARS = 8000


def extract_document_text(path: Path, *, max_chars: int = _MAX_CHARS) -> tuple[str | None, str]:
    """抽取文档文本。

    返回 (text_or_none, detail)：
    - 成功：text 非空，detail 为引擎名
    - 失败：text 为 None，detail 为原因说明
    """
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, f"读取失败: {exc}"
        return _clip(text, max_chars), "plaintext"

    if suffix == ".pdf":
        return _extract_pdf(path, max_chars=max_chars)
    if suffix in {".docx"}:
        return _extract_docx(path, max_chars=max_chars)
    if suffix in {".xlsx", ".xlsm"}:
        return _extract_xlsx(path, max_chars=max_chars)
    if suffix == ".doc":
        return None, "暂不支持旧版 .doc，请转换为 .docx"
    if suffix == ".xls":
        return None, "暂不支持旧版 .xls，请转换为 .xlsx"
    return None, f"不支持的文件类型 {suffix}"


def _clip(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…(已截断)"


def _extract_pdf(path: Path, *, max_chars: int) -> tuple[str | None, str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None, "PDF 解析需安装可选依赖: pip install 'cs-agent[docs]'（pypdf）"
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) >= max_chars:
                break
        text = _clip("\n".join(parts), max_chars)
        return (text or None), "pypdf"
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_extract_failed", path=str(path), error=str(exc))
        return None, f"PDF 解析失败: {exc}"


def _extract_docx(path: Path, *, max_chars: int) -> tuple[str | None, str]:
    try:
        import docx  # type: ignore  # python-docx
    except ImportError:
        return None, "Word 解析需安装可选依赖: pip install 'cs-agent[docs]'（python-docx）"
    try:
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text]
        text = _clip("\n".join(parts), max_chars)
        return (text or None), "python-docx"
    except Exception as exc:  # noqa: BLE001
        logger.warning("docx_extract_failed", path=str(path), error=str(exc))
        return None, f"Word 解析失败: {exc}"


def _extract_xlsx(path: Path, *, max_chars: int) -> tuple[str | None, str]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return None, "Excel 解析需安装可选依赖: pip install 'cs-agent[docs]'（openpyxl）"
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in wb.worksheets:
            lines.append(f"## Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    lines.append("\t".join(cells))
                if sum(len(x) for x in lines) >= max_chars:
                    break
            if sum(len(x) for x in lines) >= max_chars:
                break
        wb.close()
        text = _clip("\n".join(lines), max_chars)
        return (text or None), "openpyxl"
    except Exception as exc:  # noqa: BLE001
        logger.warning("xlsx_extract_failed", path=str(path), error=str(exc))
        return None, f"Excel 解析失败: {exc}"
