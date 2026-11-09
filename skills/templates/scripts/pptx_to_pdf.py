"""PowerPoint (.pptx) 转高保真 PDF 官方 CLI 工具。

用法：
    python pptx_to_pdf.py <input_pptx_path> [output_pdf_path]

调用内核与 ppt_builder.LeadPPTBuilder.to_pdf() / convert_pptx_to_pdf() 一致：
    1. LibreOffice（跨平台推荐）
    2. Windows PowerPoint COM 自动化（win32com）
    3. PowerShell PowerPoint COM 兜底
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保能导入同目录下的 ppt_builder
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ppt_builder import PdfEngineNotFoundError, convert_pptx_to_pdf


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("用法: python pptx_to_pdf.py <input_pptx_path> [output_pdf_path]")
        return 2

    pptx_path = Path(sys.argv[1]).resolve()
    if not pptx_path.is_file():
        print(f"错误: PPT 文件不存在: {pptx_path}")
        return 1

    pdf_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

    try:
        out_pdf = convert_pptx_to_pdf(pptx_path, pdf_path)
        print(f"PDF 转换成功: {out_pdf} (大小: {out_pdf.stat().st_size} bytes)")
        return 0
    except PdfEngineNotFoundError as e:
        print(f"错误: 未找到可用的 PDF 转档引擎: {e}")
        return 1
    except Exception as e:
        print(f"错误: 转换失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
