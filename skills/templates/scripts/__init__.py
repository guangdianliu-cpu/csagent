"""利得基金官方 Office 模板与高保真导出引擎。

包含：
  - LeadPPTBuilder: 官方 PPT 构建器（支持封面、目录、指标卡片、单大图、图表、左右分栏、对比页、表格与官方封底）
  - LeadDocBuilder: 官方 Word 研报构建器（支持红头抬头发函、深蓝表格、图表嵌入、分页符与末尾四项法律声明）
  - convert_docx_to_pdf: Word 转高保真 PDF
  - lead_chart_style: Matplotlib 利得深蓝主题与快速金融绘图函数
"""

from __future__ import annotations

from .doc_builder import LeadDocBuilder, PdfEngineNotFoundError, convert_docx_to_pdf
from .lead_chart_style import (
    COLOR_ACCENT1,
    COLOR_ACCENT2,
    COLOR_ACCENT3,
    COLOR_PRIMARY,
    PALETTE,
    apply_lead_theme,
    plot_bar_chart,
    plot_line_chart,
    plot_pie_chart,
)
from .ppt_builder import LeadPPTBuilder, convert_pptx_to_pdf

__all__ = [
    "LeadDocBuilder",
    "LeadPPTBuilder",
    "convert_docx_to_pdf",
    "convert_pptx_to_pdf",
    "PdfEngineNotFoundError",
    "apply_lead_theme",
    "plot_line_chart",
    "plot_bar_chart",
    "plot_pie_chart",
    "COLOR_PRIMARY",
    "COLOR_ACCENT1",
    "COLOR_ACCENT2",
    "COLOR_ACCENT3",
    "PALETTE",
]
