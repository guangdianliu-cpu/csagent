"""PDF 渲染预览工具：把 PDF 前几页转成 PNG 图像，供人工目检或交付预览。

用法：
    python pdf_preview.py <pdf_path> [out_dir] [max_pages]

引擎优先级：
    1. pdftoppm（poppler，Linux 生产机：apt install poppler-utils）
    2. pymupdf（pip install pymupdf）
    3. LibreOffice Draw（仅首页，复用 docx→pdf 的 soffice 环境）
都不可用时打印明确的跳过说明并以 0 退出——自查是增强环节，
不应因为缺工具阻断主流程。

注：生成的 PNG 预览图为二进制图像文件，纯文本工具（如 read_file）无法读取。
如需自动化文档与文稿结构自检，Word 请使用 check_doc.py，PPT 请使用 check_ppt.py。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _render_pdftoppm(pdf: Path, out_dir: Path, max_pages: int) -> list[Path]:
    exe = shutil.which("pdftoppm")
    if not exe:
        return []
    prefix = out_dir / pdf.stem
    subprocess.run(
        [exe, "-png", "-r", "110", "-f", "1", "-l", str(max_pages), str(pdf), str(prefix)],
        capture_output=True,
        timeout=120,
        check=True,
    )
    return sorted(out_dir.glob(f"{pdf.stem}-*.png"))


def _render_pymupdf(pdf: Path, out_dir: Path, max_pages: int) -> list[Path]:
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # pymupdf
    except ImportError:
        return []
    outs: list[Path] = []
    with fitz.open(str(pdf)) as doc:
        for i in range(min(max_pages, len(doc))):
            pix = doc[i].get_pixmap(dpi=110)
            out = out_dir / f"{pdf.stem}-p{i + 1}.png"
            pix.save(str(out))
            outs.append(out)
    return outs


def _render_soffice(pdf: Path, out_dir: Path) -> list[Path]:
    """用 LibreOffice Draw 渲染首页（soffice 转 PDF 为 PNG 只出第一页）。"""
    candidates = [
        shutil.which("soffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
    ]
    exe = next((c for c in candidates if c and Path(c).is_file()), None)
    if exe is None:
        return []
    subprocess.run(
        [exe, "--headless", "--convert-to", "png", "--outdir", str(out_dir), str(pdf)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    gen = out_dir / f"{pdf.stem}.png"
    return [gen] if gen.is_file() else []


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("用法: python pdf_preview.py <pdf_path> [out_dir] [max_pages]")
        return 2

    pdf = Path(sys.argv[1]).resolve()
    if not pdf.is_file():
        print(f"错误: PDF 不存在: {pdf}")
        return 1

    out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else pdf.parent / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    max_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    pages = (
        _render_pdftoppm(pdf, out_dir, max_pages)
        or _render_pymupdf(pdf, out_dir, max_pages)
        or _render_soffice(pdf, out_dir)
    )
    if not pages:
        print(
            "跳过渲染自查：未检测到 pdftoppm、pymupdf 或 LibreOffice。\n"
            "  • Linux 生产机：apt install poppler-utils\n"
            "  • 任意环境：pip install pymupdf"
        )
        return 0

    print(f"已渲染 {len(pages)} 页预览图至: {out_dir}")
    print("（提示: 如需视觉质检，可直接调用 view_image 抽查关键页面排版；如需原生代码结构核验，Word 请调用 check_doc.py，PPT 请调用 check_ppt.py）")
    for p in pages:
        try:
            rel = p.relative_to(Path.cwd()).as_posix()
            print(f"  • {rel}")
        except ValueError:
            print(f"  • {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
