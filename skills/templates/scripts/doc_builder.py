"""利得基金官方 Word 报告构建器 (LeadDocBuilder)。

基于 `skills/templates/leadfund_word_template.docx` 母版构建。
严格遵循官方规范：
  - 自动保留页眉红头（上海利得基金销售有限公司）
  - 自动更新官方研报四格抬头表（报告类型、机构、日期）
  - 标题、正文、要点统一套用利得 VI 配色（主色 #40527E、正文 #505359）
  - 表格自动套用标准深蓝表头与斑马纹底色，支持单元格合并与列宽自适应
  - 深度支持 Matplotlib 300 DPI 图表与本地图片高清插入 (add_chart_image)
  - 支持章节前显式分页控制 (add_page_break)
  - 报告末尾自动保留官方《法律声明》《风险提示》《重要声明》《保密条款》
  - 支持一键导出官方高保真 PDF (to_pdf)，自适应 LibreOffice / Word COM
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import docx
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

# ==============================================================================
# 利得官方配色规范
# ==============================================================================
COLOR_PRIMARY = RGBColor(0x40, 0x52, 0x7E)  # #40527E 深蓝
COLOR_TEXT = RGBColor(0x50, 0x53, 0x59)     # #505359 正文
COLOR_MUTED = RGBColor(0x6D, 0x71, 0x7C)    # #6D717C 注释与图题
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
COLOR_BORDER = RGBColor(0xDC, 0xE1, 0xEB)   # #DCE1EB 浅灰边框
COLOR_BORDER_HEX = "DCE1EB"
COLOR_CARD_BG_HEX = "F2F5FA"

DEFAULT_FONT = "微软雅黑"


def _display_width(s: Any) -> int:
    """计算字符串在排版中的显示权重（中文字符/东亚宽字符计为 2，半角/ASCII 计为 1）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


def _set_table_borders(table: Any, color: str = COLOR_BORDER_HEX, sz: str = "4") -> None:
    """设置标准浅灰细线边框，避免渲染器下表格文字悬空。"""
    tblPr = table._tbl.tblPr
    existing = tblPr.find(qn("w:tblBorders"))
    if existing is not None:
        tblPr.remove(existing)
    tblBorders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>\n'
        f'  <w:top w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'  <w:bottom w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'  <w:left w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'  <w:right w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'  <w:insideH w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'  <w:insideV w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>\n'
        f'</w:tblBorders>'
    )
    tblPr.append(tblBorders)


def _set_run_font(run: Any, name: str = DEFAULT_FONT) -> None:
    """设置 run 的中英文字体。

    只写 ``run.font.name`` 时 python-docx 仅设置 w:rFonts 的 ascii/hAnsi（西文）；
    中文字形走 w:eastAsia，不设置会落到模板主题的东亚字体（常见为等线），
    导致同一段落中西文字体不一致。两者必须同时设置。
    """
    run.font.name = name
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    r_fonts.set(qn("w:eastAsia"), name)


def _set_outline_level(p: Any, level: int) -> None:
    """设置段落大纲级别（0=一级），使标题进入 Word 导航窗格、可生成自动目录。"""
    p_pr = p._p.get_or_add_pPr()
    outline = OxmlElement("w:outlineLvl")
    outline.set(qn("w:val"), str(level))
    p_pr.append(outline)


DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "leadfund_word_template.docx"
)


class PdfEngineNotFoundError(RuntimeError):
    """Word 转 PDF 引擎缺失错误。"""


def _find_soffice_binary() -> Path | None:
    """寻找系统安装的 LibreOffice / OpenOffice 可执行文件。"""
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/usr/local/bin/soffice",
    ]
    for c in candidates:
        if c:
            p = Path(c)
            if p.is_file():
                return p
    return None


def convert_docx_to_pdf(docx_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    """将 Word 文档转换为高保真 PDF。

    自适应策略：
      1. LibreOffice（跨平台推荐：Linux 执行 apt/yum install libreoffice-nogui）
      2. Windows Word COM（当 Windows 系统已安装 Microsoft Word 时）
    """
    docx_file = Path(docx_path).resolve()
    if not docx_file.is_file():
        raise FileNotFoundError(f"源 Word 文件不存在: {docx_file}")

    if pdf_path is None:
        out_pdf = docx_file.with_suffix(".pdf")
    else:
        out_pdf = Path(pdf_path).resolve()

    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    # 1. 尝试 LibreOffice
    soffice_bin = _find_soffice_binary()
    if soffice_bin is not None:
        cmd = [
            str(soffice_bin),
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_pdf.parent),
            str(docx_file),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
        if proc.returncode == 0:
            gen_pdf = out_pdf.parent / f"{docx_file.stem}.pdf"
            if gen_pdf.is_file():
                if gen_pdf.resolve() != out_pdf.resolve():
                    if out_pdf.exists():
                        out_pdf.unlink()
                    gen_pdf.rename(out_pdf)
                return out_pdf

    # 2. 尝试 Python win32com (Windows 原生 COM，支持 MS Word 与 WPS Office)
    if sys.platform.startswith("win"):
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            try:
                doc = word.Documents.Open(str(docx_file))
                doc.SaveAs(str(out_pdf), FileFormat=17)
                doc.Close()
            finally:
                try:
                    word.Quit()
                except Exception:
                    pass
            if out_pdf.is_file() and out_pdf.stat().st_size > 100:
                return out_pdf
        except Exception:
            pass

    # 3. 尝试 Windows Word COM (通过 PowerShell 备用调用)
    if sys.platform.startswith("win"):
        ps_script = (
            f"$w = New-Object -ComObject Word.Application; "
            f"$w.Visible = $false; "
            f"try {{ "
            f"  $doc = $w.Documents.Open('{docx_file}'); "
            f"  $doc.SaveAs([ref]'{out_pdf}', [ref]17); "
            f"  $doc.Close(); "
            f"}} finally {{ "
            f"  $w.Quit(); "
            f"}}"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if proc.returncode == 0 and out_pdf.is_file() and out_pdf.stat().st_size > 100:
            return out_pdf

    raise PdfEngineNotFoundError(
        "未检测到可用的 Word 转 PDF 引擎。\n"
        "如需一键将 Word 转换为官方 PDF，请任选一种方式配置：\n"
        "  • Linux 生产机：安装 LibreOffice（例如 apt install libreoffice-nogui 或 yum install libreoffice-headless）\n"
        "  • Windows 开发机：安装 LibreOffice 或 Microsoft Word。"
    )


def _set_cell_bg(cell: Any, hex_color: str) -> None:
    """设置单元格背景颜色。"""
    tc_pr = cell._element.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>')
    tc_pr.append(shd)


def _set_cell_margins(cell: Any, top: int = 120, bottom: int = 120, left: int = 150, right: int = 150) -> None:
    """设置单元格内边距（单位：dxa）。"""
    tc_pr = cell._element.get_or_add_tcPr()
    tc_mar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tc_pr.append(tc_mar)


class LeadDocBuilder:
    """利得基金标准 Word 报告构建器。"""

    def __init__(self, template_path: str | Path | None = None) -> None:
        self.template_path = Path(template_path or DEFAULT_TEMPLATE_PATH)
        if not self.template_path.is_file():
            alt = self.template_path.parent / "【利得基金】word模板.docx"
            if alt.is_file():
                self.template_path = alt
            else:
                raise FileNotFoundError(f"未找到官方 Word 模板: {self.template_path}")

        self.doc = docx.Document(str(self.template_path))

        # 定位末尾法律声明锚点；找不到必须 fail loud——否则正文会被静默排到
        # 免责声明后面，产物结构错误却很难被察觉。
        self._anchor_p = None
        first_disclaimer_idx = -1
        for i, p in enumerate(self.doc.paragraphs):
            if "法律声明" in p.text:
                self._anchor_p = p
                first_disclaimer_idx = i
                break

        if self._anchor_p is None:
            raise RuntimeError(
                f"模板 {self.template_path.name} 中未找到『法律声明』锚点段落，"
                "无法保证正文插入到官方声明之前。请检查官方 Word 模板是否被改动。"
            )

        for p in list(self.doc.paragraphs[:first_disclaimer_idx]):
            if not p.text.strip():
                p._element.getparent().remove(p._element)

        for p in list(self.doc.paragraphs[first_disclaimer_idx + 1:]):
            if not p.text.strip():
                p._element.getparent().remove(p._element)

    def set_meta(
        self,
        report_type: str = "基金研究报告",
        department: str = "利得研究",
        company: str = "上海利得基金销售有限公司基金报告",
        date: str | None = None,
    ) -> None:
        """更新官方抬头四格表信息。"""
        if not date:
            today = datetime.date.today()
            date = f"{today.year}年{today.month}月{today.day}日"

        if self.doc.tables:
            table0 = self.doc.tables[0]
            try:
                table0.cell(1, 1).text = department
                table0.cell(1, 2).text = report_type
                table0.cell(2, 1).text = company
                table0.cell(2, 2).text = date
                for r in (1, 2):
                    for c in (1, 2):
                        cell = table0.cell(r, c)
                        for p in cell.paragraphs:
                            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                            for run in p.runs:
                                run.font.size = Pt(10.5)
                                # 抬头表单元格底纹为深蓝 #40527E，文字必须为白色，
                                # 否则深灰字压深蓝底不可读（模板原始 run 即为 FFFFFF）
                                run.font.color.rgb = COLOR_WHITE
                                _set_run_font(run)
            except Exception:
                pass

    def add_title(self, title: str, subtitle: str = "") -> None:
        """添加研报大标题与副标题。"""
        p = self._anchor_p.insert_paragraph_before()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_before = Pt(16)
        p.paragraph_format.space_after = Pt(6)
        run = p.add_run(title)
        run.font.size = Pt(22)
        run.font.bold = True
        run.font.color.rgb = COLOR_PRIMARY
        _set_run_font(run)

        if subtitle:
            p_sub = self._anchor_p.insert_paragraph_before()
            p_sub.paragraph_format.space_after = Pt(14)
            r_sub = p_sub.add_run(subtitle)
            r_sub.font.size = Pt(11)
            r_sub.font.color.rgb = COLOR_MUTED
            _set_run_font(r_sub)

    def add_heading_1(self, text: str) -> None:
        """添加一级标题（15pt 加粗 利得深蓝），进入 Word 导航窗格 / 可生成目录。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_before = Pt(16)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = True
        _set_outline_level(p, 0)
        run = p.add_run(text)
        run.font.size = Pt(15)
        run.font.bold = True
        run.font.color.rgb = COLOR_PRIMARY
        _set_run_font(run)

    def add_heading_2(self, text: str) -> None:
        """添加二级标题（12.5pt 加粗），进入 Word 导航窗格 / 可生成目录。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True
        _set_outline_level(p, 1)
        run = p.add_run(text)
        run.font.size = Pt(12.5)
        run.font.bold = True
        run.font.color.rgb = COLOR_PRIMARY
        _set_run_font(run)

    def add_heading_3(self, text: str) -> None:
        """添加三级标题（11pt 加粗 正文深色），进入 Word 导航窗格 / 可生成目录。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True
        _set_outline_level(p, 2)
        run = p.add_run(text)
        run.font.size = Pt(11)
        run.font.bold = True
        run.font.color.rgb = COLOR_TEXT
        _set_run_font(run)

    def add_paragraph(
        self,
        text: str,
        bold_prefix: str = "",
        indent: bool = True,
        justify: bool = True,
    ) -> Any:
        """添加正文段落。

        遵循中文正式研报排版规范 (P0)：
          - 默认首行缩进 2 字符 (10.5pt 五号字下折算约为 21pt / 2 em)
          - 默认两端对齐 (WD_ALIGN_PARAGRAPH.JUSTIFY)
          - 可通过 indent=False / justify=False 按需关闭
        """
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_after = Pt(8)
        p.paragraph_format.line_spacing = 1.3
        if justify:
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        if indent:
            p.paragraph_format.first_line_indent = Pt(21)

        if bold_prefix:
            r_b = p.add_run(bold_prefix)
            r_b.font.size = Pt(10.5)
            r_b.font.bold = True
            r_b.font.color.rgb = COLOR_PRIMARY
            _set_run_font(r_b)

        run = p.add_run(text)
        run.font.size = Pt(10.5)
        run.font.color.rgb = COLOR_TEXT
        _set_run_font(run)
        return p

    def add_bullet(self, text: str, bold_prefix: str = "") -> None:
        """添加项目要点。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.25

        dot = p.add_run("•  ")
        dot.font.color.rgb = COLOR_PRIMARY
        dot.font.bold = True
        _set_run_font(dot)

        if bold_prefix:
            r_b = p.add_run(bold_prefix)
            r_b.font.size = Pt(10.5)
            r_b.font.bold = True
            r_b.font.color.rgb = COLOR_PRIMARY
            _set_run_font(r_b)

        run = p.add_run(text)
        run.font.size = Pt(10.5)
        run.font.color.rgb = COLOR_TEXT
        _set_run_font(run)

    def add_numbered_item(self, index: int, text: str, bold_prefix: str = "") -> None:
        """添加带编号要点（与 add_bullet 同版式，序号使用利得深蓝加粗）。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.25

        num = p.add_run(f"{index}.  ")
        num.font.color.rgb = COLOR_PRIMARY
        num.font.bold = True
        num.font.size = Pt(10.5)
        _set_run_font(num)

        if bold_prefix:
            r_b = p.add_run(bold_prefix)
            r_b.font.size = Pt(10.5)
            r_b.font.bold = True
            r_b.font.color.rgb = COLOR_PRIMARY
            _set_run_font(r_b)

        run = p.add_run(text)
        run.font.size = Pt(10.5)
        run.font.color.rgb = COLOR_TEXT
        _set_run_font(run)

    def add_callout(
        self,
        text: str,
        title: str = "核心观点",
        width_inches: float = 6.2,
    ) -> Any:
        """添加带浅蓝底纹和深蓝左侧强调粗边的核心观点引用块。"""
        table = self.doc.add_table(1, 1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        cell = table.cell(0, 0)
        cell.width = Inches(width_inches)
        table.columns[0].width = Inches(width_inches)
        _set_cell_bg(cell, COLOR_CARD_BG_HEX)
        _set_cell_margins(cell, top=140, bottom=140, left=200, right=200)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER

        # 左侧利得深蓝强调粗边框 (4.5pt)，其余无边框
        tblPr = table._tbl.tblPr
        tblBorders = parse_xml(
            f'<w:tblBorders {nsdecls("w")}>\n'
            f'  <w:top w:val="none"/>\n'
            f'  <w:left w:val="single" w:sz="36" w:space="0" w:color="40527E"/>\n'
            f'  <w:bottom w:val="none"/>\n'
            f'  <w:right w:val="none"/>\n'
            f'</w:tblBorders>'
        )
        tblPr.append(tblBorders)

        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.3
        r_t = p.add_run(f"【{title}】 ")
        r_t.font.bold = True
        r_t.font.size = Pt(10.5)
        r_t.font.color.rgb = COLOR_PRIMARY
        _set_run_font(r_t)

        r = p.add_run(text)
        r.font.size = Pt(10.5)
        r.font.color.rgb = COLOR_TEXT
        _set_run_font(r)

        self._anchor_p._p.addprevious(table._tbl)
        return table

    def add_kpi_cards(
        self,
        cards: Sequence[dict[str, str]],
        total_width_inches: float = 6.2,
    ) -> Any:
        """添加关键指标卡片格栅（Key Metrics Grid）。

        支持 2~4 个指标卡片并排展示，统一大字号核心数值与小字号标签说明，
        适用于基金经理画像、产品核心特征、宏观指标等卡片速览。

        :param cards: 卡片字典列表，每个元素建议包含：
            - value: 核心大字号数值（如 "135.0 亿"、"15.5%"、"12.6 年"）
            - label: 指标名称（如 "在管权益规模"、"生涯年化回报"）
            - sub: (可选) 补充说明（如 "8只主动权益"、"长期复利显著"）
        :param total_width_inches: 卡片行总宽度（英寸，默认 6.2）
        """
        num_cards = len(cards)
        if num_cards == 0:
            return None

        table = self.doc.add_table(1, num_cards)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        _set_table_borders(table, color=COLOR_BORDER_HEX, sz="4")

        col_w = round(total_width_inches / num_cards, 2)
        for c_idx, card in enumerate(cards):
            cell = table.cell(0, c_idx)
            cell.width = Inches(col_w)
            _set_cell_bg(cell, "F7F9FC")
            _set_cell_margins(cell, top=120, bottom=120, left=80, right=80)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER

            p0 = cell.paragraphs[0]
            p0.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p0.paragraph_format.space_before = Pt(0)
            p0.paragraph_format.space_after = Pt(2)
            r_val = p0.add_run(str(card.get("value", "")))
            r_val.font.size = Pt(16)
            r_val.font.bold = True
            r_val.font.color.rgb = COLOR_PRIMARY
            _set_run_font(r_val)

            p1 = cell.add_paragraph()
            p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p1.paragraph_format.space_before = Pt(0)
            p1.paragraph_format.space_after = Pt(0)
            p1.paragraph_format.line_spacing = 1.15
            r_lbl = p1.add_run(str(card.get("label", "")))
            r_lbl.font.size = Pt(9.5)
            r_lbl.font.bold = True
            r_lbl.font.color.rgb = COLOR_TEXT
            _set_run_font(r_lbl)

            sub = card.get("sub", "")
            if sub:
                p2 = cell.add_paragraph()
                p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p2.paragraph_format.space_before = Pt(1)
                p2.paragraph_format.space_after = Pt(0)
                r_sub = p2.add_run(str(sub))
                r_sub.font.size = Pt(8.5)
                r_sub.font.color.rgb = COLOR_MUTED
                _set_run_font(r_sub)

        for c_idx in range(num_cards):
            table.columns[c_idx].width = Inches(col_w)

        self._anchor_p._p.addprevious(table._tbl)
        return table

    def add_chart_image(
        self,
        image_path: str | Path,
        caption: str = "",
        width_inches: float = 5.8,
    ) -> None:
        """在报告中居中插入图表或图片，并带标准灰色斜体图题 (P1)。"""
        img_p = Path(image_path).resolve()
        if not img_p.is_file():
            raise FileNotFoundError(f"图表文件不存在: {img_p}")

        p_img = self._anchor_p.insert_paragraph_before()
        p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_img.paragraph_format.space_before = Pt(8)
        p_img.paragraph_format.space_after = Pt(4)
        run_img = p_img.add_run()
        run_img.add_picture(str(img_p), width=Inches(width_inches))

        if caption:
            p_cap = self._anchor_p.insert_paragraph_before()
            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_cap.paragraph_format.space_after = Pt(10)
            r_cap = p_cap.add_run(caption)
            r_cap.font.size = Pt(9.5)
            r_cap.font.color.rgb = COLOR_MUTED
            r_cap.font.italic = True
            _set_run_font(r_cap)

    def add_page_break(self) -> None:
        """插入分页符，保证大章节另起新页 (P3)。"""
        p = self._anchor_p.insert_paragraph_before()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run()
        run.add_break(WD_BREAK.PAGE)

    def add_table(
        self,
        headers: list[str],
        rows: list[list[Any]],
        col_widths: list[float] | None = None,
        merge_cells: Sequence[tuple[int, int, int, int]] | None = None,
        border_color: str | None = COLOR_BORDER_HEX,
        total_width_inches: float = 6.2,
    ) -> Any:
        """添加标准深蓝样式表格，支持单元格合并、细浅灰网格边框与列宽自适应 (P0/P3)。

        :param headers: 表头列名列表
        :param rows: 行数据列表
        :param col_widths: 手动指定的列宽列表（英寸）。若为 None 则根据内容长度自适应计算
        :param merge_cells: 单元格合并坐标列表，格式 [(r1, c1, r2, c2), ...]
        :param border_color: 边框十六进制色值（默认 #DCE1EB 浅灰边框，None 为不显式设边框）
        :param total_width_inches: 表格在 A4 版心中的总安全宽度（默认 6.2 英寸，防止宽表溢出）
        """
        num_rows = len(rows) + 1
        num_cols = len(headers)
        table = self.doc.add_table(num_rows, num_cols)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        # 1. 边框控制 (P0：细浅灰边框，防止文字悬空)
        if border_color:
            _set_table_borders(table, color=border_color)

        # 2. 表头
        for col_idx, h in enumerate(headers):
            cell = table.cell(0, col_idx)
            cell.text = str(h)
            _set_cell_bg(cell, "40527E")
            _set_cell_margins(cell, top=120, bottom=120, left=100, right=100)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            for p in cell.paragraphs:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for r in p.runs:
                    r.font.size = Pt(9.5)
                    r.font.bold = True
                    r.font.color.rgb = COLOR_WHITE
                    _set_run_font(r)

        # 表格跨页时在新页重复表头行（w:tblHeader）
        tr_pr0 = table.rows[0]._tr.get_or_add_trPr()
        tr_pr0.append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))

        # 3. 数据行
        for row_idx, r_data in enumerate(rows, start=1):
            bg = "F7F9FC" if row_idx % 2 == 1 else "FFFFFF"
            for col_idx, val in enumerate(r_data):
                cell = table.cell(row_idx, col_idx)
                cell.text = str(val if val is not None else "-")
                _set_cell_bg(cell, bg)
                _set_cell_margins(cell, top=100, bottom=100, left=100, right=100)
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                for p in cell.paragraphs:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for r in p.runs:
                        r.font.size = Pt(9.5)
                        r.font.color.rgb = COLOR_TEXT
                        _set_run_font(r)

        # 为所有行添加跨页防截断标记（w:cantSplit）
        for row in table.rows:
            tr_r = row._tr.get_or_add_trPr()
            tr_r.append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))

        # 4. 合并单元格 (P3)
        if merge_cells:
            for r1, c1, r2, c2 in merge_cells:
                table.cell(r1, c1).merge(table.cell(r2, c2))

        # 5. 列宽自适应计算与分配 (P0)
        # 若未指定 col_widths，按列文本字符东亚显示权重动态分配，总宽度不超过 total_width_inches
        calculated_widths: list[float]
        if col_widths and len(col_widths) == num_cols:
            calculated_widths = list(col_widths)
        else:
            weights = []
            for col_idx in range(num_cols):
                header_len = _display_width(headers[col_idx])
                max_cell_len = max(
                    (_display_width(rows[r][col_idx]) for r in range(len(rows)) if col_idx < len(rows[r])),
                    default=0,
                )
                weight = max(header_len, max_cell_len, 4)
                weights.append(weight)
            total_w = sum(weights) or 1
            calculated_widths = [round((w / total_w) * total_width_inches, 2) for w in weights]
            diff = round(total_width_inches - sum(calculated_widths), 2)
            if diff != 0:
                calculated_widths[-1] = max(0.5, round(calculated_widths[-1] + diff, 2))

        for col_idx, w in enumerate(calculated_widths):
            for row in table.rows:
                row.cells[col_idx].width = Inches(w)
            table.columns[col_idx].width = Inches(w)

        # 挂载到免责声明前面
        self._anchor_p._p.addprevious(table._tbl)

        return table

    def save(self, output_path: str | Path) -> Path:
        """保存 Word 文档。"""
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out))
        return out

    def to_pdf(self, output_pdf_path: str | Path | None = None) -> Path:
        """保存 Word 并直接转换为高保真官方 PDF (P0)。"""
        if output_pdf_path is None:
            temp_docx = Path("data/uploads/outbound/temp_report.docx").resolve()
            out_pdf = temp_docx.with_suffix(".pdf")
        else:
            out_pdf = Path(output_pdf_path).resolve()
            temp_docx = out_pdf.with_suffix(".docx")

        self.save(temp_docx)
        return convert_docx_to_pdf(temp_docx, out_pdf)
