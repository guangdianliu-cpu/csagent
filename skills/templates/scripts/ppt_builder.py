"""利得基金官方 PPT 构建器 (LeadPPTBuilder)。

基于 `skills/templates/leadfund_ppt_template.pptx` 母版构建。
严格遵循官方 VI 视觉规范：
  - 主题主色：#40527E（利得深蓝）
  - 辅助配色序列：#40527E, #819DB8, #C3CBD2, #38768F, #AF5B5B, #73608E, #80BFBE, #B5BC54, #E19056, #858C98
  - 正文颜色：#505359，辅助注释：#6D717C
  - 字号规范：大标题 20pt 加粗、中标题 16-18pt、正文 12-14pt、注释 9-10pt
  - 自动集成官方母版背景、LOGO、页面编号与官方封底

版式支持库 (P1/P2)：
  1. 官方封面页 (set_cover)
  2. 目录大纲页 (add_agenda_slide) - 对齐母版 Slide 1
  3. 核心指标卡片页 (add_cards_slide) - 对齐母版 Slide 10
  4. 投研图表与单大图页 (add_chart_slide / add_image_slide)
  5. 左右分栏图文混排页 (add_two_column_slide) - 对齐母版 Slide 12
  6. 双对象对比页 (add_comparison_slide)
  7. 标准数据表格页 (add_table_slide) - 对齐母版 Slide 9
  8. 正文与要点页 (add_content_slide)
  9. 官方封底 (自动置底)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.opc.packuri import PackURI
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

# ==============================================================================
# 利得官方 VI 配色规范（源自 PPT 模板 Slide 4）
# ==============================================================================
COLOR_PRIMARY = RGBColor(0x40, 0x52, 0x7E)  # #40527E 官方主色（深蓝）
COLOR_TEXT = RGBColor(0x50, 0x53, 0x59)     # #505359 正文色
COLOR_MUTED = RGBColor(0x6D, 0x71, 0x7C)    # #6D717C 辅助与坐标轴
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
COLOR_CARD_BG = RGBColor(0xF7, 0xF9, 0xFC)  # 卡片浅灰底
COLOR_BORDER = RGBColor(0xDC, 0xE1, 0xEB)   # 细边框色

# 官方 10 色主题序列
PALETTE = [
    RGBColor(0x40, 0x52, 0x7E),  # 1. 主深蓝
    RGBColor(0x38, 0x76, 0x8F),  # 2. 青蓝
    RGBColor(0x81, 0x9D, 0xB8),  # 3. 灰蓝
    RGBColor(0xAF, 0x5B, 0x5B),  # 4. 砖红
    RGBColor(0xE1, 0x90, 0x56),  # 5. 暖橙
    RGBColor(0x80, 0xBF, 0xBE),  # 6. 薄荷
    RGBColor(0x73, 0x60, 0x8E),  # 7. 紫灰
    RGBColor(0xB5, 0xBC, 0x54),  # 8. 草绿
    RGBColor(0x85, 0x8C, 0x98),  # 9. 中灰
    RGBColor(0xC3, 0xCB, 0xD2),  # 10. 银灰
]

DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "leadfund_ppt_template.pptx"
)


class PdfEngineNotFoundError(RuntimeError):
    """PPT 转 PDF 引擎缺失错误。"""


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


def convert_pptx_to_pdf(pptx_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    """将 PPT 演示文稿转换为高保真 PDF (P0)。

    自适应策略：
      1. LibreOffice（跨平台推荐）
      2. Windows PowerPoint COM（当 Windows 系统已安装 Microsoft Office 时）
    """
    pptx_file = Path(pptx_path).resolve()
    if not pptx_file.is_file():
        raise FileNotFoundError(f"源 PPT 文件不存在: {pptx_file}")

    if pdf_path is None:
        out_pdf = pptx_file.with_suffix(".pdf")
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
            str(pptx_file),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, check=False)
            gen_pdf = out_pdf.parent / f"{pptx_file.stem}.pdf"
            if gen_pdf.is_file() and gen_pdf.stat().st_size > 100:
                if gen_pdf != out_pdf:
                    shutil.move(str(gen_pdf), str(out_pdf))
                return out_pdf
        except Exception:
            pass

    # 2. 尝试 Windows PowerPoint COM (pywin32)
    if sys.platform.startswith("win"):
        try:
            import win32com.client  # type: ignore
            import pythoncom  # type: ignore

            pythoncom.CoInitialize()
            ppt = win32com.client.DispatchEx("PowerPoint.Application")
            try:
                deck = ppt.Presentations.Open(str(pptx_file), True, False, False)
                deck.SaveAs(str(out_pdf), 32)  # ppSaveAsPDF = 32
                deck.Close()
            finally:
                try:
                    ppt.Quit()
                except Exception:
                    pass
            if out_pdf.is_file() and out_pdf.stat().st_size > 100:
                return out_pdf
        except Exception:
            pass

    # 3. 尝试 Windows PowerPoint COM (PowerShell 备用调用)
    if sys.platform.startswith("win"):
        ps_script = (
            f"$p = New-Object -ComObject PowerPoint.Application; "
            f"try {{ "
            f"  $d = $p.Presentations.Open('{pptx_file}', $true, $false, $false); "
            f"  $d.SaveAs([ref]'{out_pdf}', 32); "
            f"  $d.Close(); "
            f"}} finally {{ "
            f"  $p.Quit(); "
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
        "未检测到可用的 PPT 转 PDF 引擎。\n"
        "如需一键将 PPT 转换为 PDF（供目检或交付），请任选一种方式配置：\n"
        "  • Linux 生产机：安装 LibreOffice（例如 apt install libreoffice-nogui 或 yum install libreoffice-headless）\n"
        "  • Windows 开发机：安装 LibreOffice 或 Microsoft Office PowerPoint。"
    )


def _set_ppt_font(target: Any, name: str = "微软雅黑") -> None:
    """设置文本框段落、run 或 font 的中英文字体。"""
    try:
        font = target.font if hasattr(target, "font") else target
        font.name = name
        r_pr = getattr(font, "_rPr", None)
        if r_pr is not None:
            ea = r_pr.find(qn("a:ea"))
            if ea is None:
                ea = r_pr.makeelement(qn("a:ea"), {})
                latin = r_pr.find(qn("a:latin"))
                if latin is not None:
                    latin.addnext(ea)
                else:
                    r_pr.append(ea)
            ea.set("typeface", name)
    except Exception:
        pass


class LeadPPTBuilder:
    """利得基金标准 PPT 构建器。"""

    def __init__(self, template_path: str | Path | None = None) -> None:
        self.template_path = Path(template_path or DEFAULT_TEMPLATE_PATH)
        if not self.template_path.is_file():
            alt = self.template_path.parent / "利得基金PPT模板_2026新版.pptx"
            if alt.is_file():
                self.template_path = alt
            else:
                raise FileNotFoundError(f"未找到官方 PPT 模板: {self.template_path}")

        self.prs = Presentation(str(self.template_path))
        self.slide_width = self.prs.slide_width
        self.slide_height = self.prs.slide_height
        self._content_layout = self.prs.slide_layouts[1]

        # 初始化：保留 Slide 0 封面与 Slide 14 封底，释放 1..13 示例页
        self._has_closing = False
        self._closing_sld_id = None
        if len(self.prs.slides) >= 15:
            self._has_closing = True
            for idx in range(13, 0, -1):
                sld_id = self.prs.slides._sldIdLst[idx]
                self.prs.part.drop_rel(sld_id.rId)
                del self.prs.slides._sldIdLst[idx]
            self._closing_sld_id = self.prs.slides._sldIdLst[1]
            self._park_closing_partname()

    def _park_closing_partname(self) -> None:
        """把封底部件改挂到 ``slide2.xml``。

        打开模板时 ``slides`` 会把各页重命名为 ``slide1``..``slideN``，封底是
        ``slideN.xml``。删掉中间示例页后这个名字还在。新页文件名是
        「当前页数 + 1」，从 3 开始只增不减，加到第 N-1 张时会再次申请
        ``slideN.xml``。两个部件同名时 PowerPoint 只画其中一份，封底就出现两次，
        撞名的那张内容页打不开。``slide2`` 这个号新页算法分配不到。
        """
        if self._closing_sld_id is None:
            return
        closing = self.prs.part.related_part(self._closing_sld_id.rId)
        parked = PackURI("/ppt/slides/slide2.xml")
        if closing.partname == parked:
            return
        closing.partname = parked

    def set_cover(self, title: str, subtitle: str = "", date: str = "") -> None:
        """配置官方封面页（Slide 0）。"""
        slide0 = self.prs.slides[0]

        # 1. 清理 Slide 0 历史底模中残留的 AlternateContent 干扰元素（如 '利得基金 • 公司册'）
        for el in list(slide0._element.iter()):
            if "AlternateContent" in el.tag:
                raw = etree.tostring(el, encoding="utf-8").decode("utf-8", errors="ignore")
                if "公司册" in raw or "科技赋能" in raw:
                    parent = el.getparent()
                    if parent is not None:
                        parent.remove(el)

        # 2. 定位主标题框与右侧角标框
        title_shape = None
        badge_shape = None
        for shape in slide0.shapes:
            if shape.has_text_frame:
                txt = shape.text.strip()
                if "科技赋能" in txt or "标题" in shape.name or shape.name == "文本框 11":
                    title_shape = shape
                elif "2026" in txt or "季度" in txt or shape.name == "矩形 12":
                    badge_shape = shape

        # 3. 设置主标题与副标题（深蓝 #40527E 显式文字，配合灰色副标题）
        if title_shape is not None:
            title_shape.left = Inches(0.38)
            title_shape.top = Inches(4.08)
            title_shape.width = Inches(7.5)
            title_shape.height = Inches(0.9)
            tf = title_shape.text_frame
            tf.word_wrap = True
            tf.clear()
            p = tf.paragraphs[0]
            p.text = title
            p.font.size = Pt(26)
            p.font.bold = True
            p.font.color.rgb = COLOR_PRIMARY
            _set_ppt_font(p.font)

            if subtitle:
                p_sub = tf.add_paragraph()
                p_sub.text = subtitle
                p_sub.font.size = Pt(13)
                p_sub.font.bold = True
                p_sub.font.color.rgb = RGBColor(0xAF, 0x5B, 0x5B)
                p_sub.space_before = Pt(6)
                _set_ppt_font(p_sub.font)

        # 4. 右侧红棕小方块角标 (0.71" x 0.71")：仅容纳紧凑版本/日期标签，严禁塞入长副标题！
        if badge_shape is not None:
            if date:
                compact_date = (
                    date.replace("年", "年\n")
                    if "年" in date and "\n" not in date
                    else date
                )
                if len(compact_date) > 10:
                    compact_date = compact_date[:10]
                badge_shape.text = compact_date
            else:
                badge_shape.text = "2026年\n3季度版"
            for p in badge_shape.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                p.font.size = Pt(9)
                p.font.bold = True
                p.font.color.rgb = COLOR_WHITE
                _set_ppt_font(p.font)

    def add_title(self, slide, text: str) -> Any:
        """为内容页统一添加标准规范标题（20pt 加粗 #40527E，对齐模板顶栏线）。"""
        tb = slide.shapes.add_textbox(Inches(0.85), Inches(0.16), Inches(7.6), Inches(0.44))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0)
        tf.margin_right = Inches(0)
        tf.margin_top = Inches(0)
        tf.margin_bottom = Inches(0)
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = COLOR_PRIMARY
        _set_ppt_font(p)
        return tb

    def add_agenda_slide(self, items: list[str], title: str = "目录 / CONTENTS") -> Any:
        """添加官方标准目录大纲页 (P2)。采用母版 Layout 3 官方特色版式。"""
        # 优先寻找官方 Layout 3 (名称精确匹配 "目录")
        agenda_layout = None
        for lay in self.prs.slide_layouts:
            if lay.name == "目录":
                agenda_layout = lay
                break
        if agenda_layout is None:
            for lay in self.prs.slide_layouts:
                if "目录" in lay.name:
                    agenda_layout = lay
                    break

        if agenda_layout is not None:
            # 清理母版 Layout 3 中遗留的多余浮动文本框（仅保留中轴砖红方块徽标 '矩形 8' 内的唯一 '目录'）
            for sh in list(agenda_layout.shapes):
                if sh.has_text_frame and "目录" in sh.text and sh.name != "矩形 8":
                    sp = sh._element
                    if sp.getparent() is not None:
                        sp.getparent().remove(sp)

            slide = self.prs.slides.add_slide(agenda_layout)
            n = len(items)
            start_x = Inches(5.95)
            start_y = Inches(1.15)
            spacing = min(Inches(3.5 / max(n, 1)), Inches(0.68))

            b_size = Inches(0.38)

            for idx, item in enumerate(items, start=1):
                y = start_y + (idx - 1) * spacing

                box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, start_x, y, b_size, b_size)
                if idx == 1:
                    box.fill.solid()
                    box.fill.fore_color.rgb = COLOR_PRIMARY
                    box.line.fill.background()
                    num_color = COLOR_WHITE
                else:
                    box.fill.solid()
                    box.fill.fore_color.rgb = COLOR_WHITE
                    box.line.color.rgb = COLOR_BORDER
                    box.line.width = Pt(1)
                    num_color = COLOR_MUTED

                box.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
                box.text_frame.margin_left = box.text_frame.margin_right = 0
                box.text_frame.margin_top = box.text_frame.margin_bottom = 0
                p_num = box.text_frame.paragraphs[0]
                p_num.text = str(idx)
                p_num.alignment = PP_ALIGN.CENTER
                p_num.font.size = Pt(13)
                p_num.font.bold = True
                p_num.font.color.rgb = num_color
                p_num.font.name = "Arial"

                # 文本框与序号方块严格同高且垂直居中，基准线完美对齐
                tb = slide.shapes.add_textbox(start_x + Inches(0.55), y, Inches(3.4), b_size)
                tf = tb.text_frame
                tf.word_wrap = True
                tf.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf.margin_left = tf.margin_right = 0
                tf.margin_top = tf.margin_bottom = 0
                p = tf.paragraphs[0]
                p.text = item
                p.font.size = Pt(15)
                p.font.bold = (idx == 1)
                p.font.color.rgb = COLOR_PRIMARY if idx == 1 else COLOR_TEXT
                _set_ppt_font(p)
            return slide
        else:
            slide = self.prs.slides.add_slide(self._content_layout)
            self.add_title(slide, title)
            n = len(items)
            top_start = Inches(1.4)
            left = Inches(1.2)
            spacing = min(Inches(3.6 / max(n, 1)), Inches(0.8))
            for idx, item in enumerate(items, start=1):
                y = top_start + (idx - 1) * spacing
                num_box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, y, Inches(0.42), Inches(0.42))
                num_box.fill.solid()
                num_box.fill.fore_color.rgb = PALETTE[(idx - 1) % len(PALETTE)]
                num_box.line.fill.background()
                p_num = num_box.text_frame.paragraphs[0]
                p_num.text = f"{idx:02d}"
                p_num.alignment = PP_ALIGN.CENTER
                p_num.font.size = Pt(12)
                p_num.font.bold = True
                p_num.font.color.rgb = COLOR_WHITE
                p_num.font.name = "Arial"

                tb = slide.shapes.add_textbox(left + Inches(0.6), y - Inches(0.05), Inches(7.0), Inches(0.5))
                p_txt = tb.text_frame.paragraphs[0]
                p_txt.text = str(item)
                p_txt.font.size = Pt(14)
                p_txt.font.bold = True
                p_txt.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p_txt)
            return slide

    def add_chart_slide(
        self,
        title: str,
        image_path: str | Path,
        notes: list[str] | str | None = None,
        subtitle: str = "",
    ) -> Any:
        """添加投研高清图表页 (P1/P2)。支持自动排版大图与核心结论。"""
        return self.add_image_slide(title, image_path, notes=notes, subtitle=subtitle)

    def add_image_slide(
        self,
        title: str,
        image_path: str | Path,
        notes: list[str] | str | None = None,
        subtitle: str = "",
    ) -> Any:
        """添加单一大图展示页 (P2)。图表居左/居中，右侧配核心研判卡片。"""
        img_p = Path(image_path).resolve()
        if not img_p.is_file():
            raise FileNotFoundError(f"未找到图片文件: {img_p}")

        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        if subtitle:
            sub_box = slide.shapes.add_textbox(
                Inches(0.85), Inches(0.78), Inches(8.3), Inches(0.32)
            )
            sp = sub_box.text_frame.paragraphs[0]
            sp.text = subtitle
            sp.font.size = Pt(10.5)
            sp.font.color.rgb = COLOR_MUTED
            _set_ppt_font(sp)

        top = Inches(1.15)
        if notes:
            # 左侧放图（宽 5.15 英寸），右侧放观察研判卡片（宽 3.0 英寸）
            slide.shapes.add_picture(str(img_p), Inches(0.85), top, width=Inches(5.15))

            # 右侧卡片（对齐 Slide 10 规范）
            right_x = Inches(6.15)
            card_w = Inches(3.00)
            card_h = Inches(3.65)
            header_h = Inches(0.40)

            # 卡片底框
            bg_rect = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, right_x, top + header_h, card_w, card_h - header_h
            )
            bg_rect.fill.solid()
            bg_rect.fill.fore_color.rgb = COLOR_CARD_BG
            bg_rect.line.color.rgb = COLOR_BORDER
            bg_rect.line.width = Pt(1)

            # 顶部实体深蓝标题栏
            hdr = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, right_x, top, card_w, header_h
            )
            hdr.fill.solid()
            hdr.fill.fore_color.rgb = COLOR_PRIMARY
            hdr.line.fill.background()
            p_hdr = hdr.text_frame.paragraphs[0]
            p_hdr.text = "核心研判与发现"
            p_hdr.font.size = Pt(12.5)
            p_hdr.font.bold = True
            p_hdr.font.color.rgb = COLOR_WHITE
            p_hdr.alignment = PP_ALIGN.CENTER
            _set_ppt_font(p_hdr)
            hdr.text_frame.margin_left = hdr.text_frame.margin_right = 0
            hdr.text_frame.margin_top = hdr.text_frame.margin_bottom = 0

            # 内部研判要点
            tb = slide.shapes.add_textbox(
                right_x + Inches(0.18), top + header_h + Inches(0.16), card_w - Inches(0.36), card_h - header_h - Inches(0.28)
            )
            tf = tb.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

            note_list = [notes] if isinstance(notes, str) else notes
            total_chars = sum(len(n) for n in note_list)
            num_notes = len(note_list)
            if num_notes > 3 or total_chars > 95:
                note_size = Pt(9.5)
                note_space = Pt(4)
            else:
                note_size = Pt(10.5)
                note_space = Pt(8)

            for idx, note in enumerate(note_list):
                p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
                p.text = f"•  {note}"
                p.font.size = note_size
                p.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p)
                p.space_after = note_space
        else:
            # 纯大图居中
            slide.shapes.add_picture(str(img_p), Inches(1.2), top, width=Inches(7.6))

        return slide

    def add_two_column_slide(
        self,
        title: str,
        left_bullets: list[str],
        right_bullets: list[str],
        left_title: str = "宏观与行业环境",
        right_title: str = "投资建议与策略",
    ) -> Any:
        """添加左右两栏对比/要点页 (P2)。对齐模板 Slide 10/12 多色模块风格。"""
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        col_w = Inches(4.05)
        col_h = Inches(3.60)
        top = Inches(1.15)
        header_h = Inches(0.44)

        for _col_idx, (col_x, head, bullets, theme_color) in enumerate([
            (Inches(0.85), left_title, left_bullets, COLOR_PRIMARY),
            (Inches(5.10), right_title, right_bullets, PALETTE[2]),
        ]):
            # 背景卡片
            card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, col_x, top + header_h, col_w, col_h - header_h)
            card.fill.solid()
            card.fill.fore_color.rgb = COLOR_CARD_BG
            card.line.color.rgb = COLOR_BORDER
            card.line.width = Pt(1)

            # 顶部实体色块标题栏
            hdr = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, col_x, top, col_w, header_h)
            hdr.fill.solid()
            hdr.fill.fore_color.rgb = theme_color
            hdr.line.fill.background()
            p_hdr = hdr.text_frame.paragraphs[0]
            p_hdr.text = head
            p_hdr.font.size = Pt(13)
            p_hdr.font.bold = True
            p_hdr.font.color.rgb = COLOR_WHITE
            p_hdr.alignment = PP_ALIGN.CENTER
            _set_ppt_font(p_hdr)
            hdr.text_frame.margin_left = hdr.text_frame.margin_right = 0
            hdr.text_frame.margin_top = hdr.text_frame.margin_bottom = 0

            tb = slide.shapes.add_textbox(
                col_x + Inches(0.20), top + header_h + Inches(0.18), col_w - Inches(0.40), col_h - header_h - Inches(0.30)
            )
            tf = tb.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

            total_chars = sum(len(b) for b in bullets)
            num_bullets = len(bullets)
            if num_bullets > 5 or total_chars > 120:
                bullet_size = Pt(9.5)
                bullet_space = Pt(4)
            elif num_bullets > 4 or total_chars > 80:
                bullet_size = Pt(10)
                bullet_space = Pt(6)
            else:
                bullet_size = Pt(10.5)
                bullet_space = Pt(8)

            for idx, b in enumerate(bullets):
                p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
                p.text = f"•  {b}"
                p.font.size = bullet_size
                p.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p)
                p.space_after = bullet_space

        return slide

    def add_comparison_slide(
        self,
        title: str,
        item_a: dict[str, Any],
        item_b: dict[str, Any],
    ) -> Any:
        """添加双对象深度对比页 (P2)。适合两只基金、两套策略或两个季度的对比。

        item 格式: {"name": "产品A", "metrics": [("规模", "120亿"), ("收益", "+24%")], "pros": ["优势1", ...]}
        """
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        col_w = Inches(4.0)
        col_h = Inches(3.6)
        top = Inches(1.3)

        for col_idx, (col_x, item, theme_color) in enumerate([
            (Inches(0.8), item_a, COLOR_PRIMARY),
            (Inches(5.2), item_b, PALETTE[3]),  # 砖红色对比
        ]):
            card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, col_x, top, col_w, col_h)
            card.fill.solid()
            card.fill.fore_color.rgb = COLOR_CARD_BG
            card.line.color.rgb = COLOR_BORDER
            card.line.width = Pt(1)

            # 顶部标题栏
            bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, col_x, top, col_w, Inches(0.5))
            bar.fill.solid()
            bar.fill.fore_color.rgb = theme_color
            bar.line.fill.background()
            p_bar = bar.text_frame.paragraphs[0]
            p_bar.text = str(item.get("name", f"方案 {col_idx + 1}"))
            p_bar.font.size = Pt(13)
            p_bar.font.bold = True
            p_bar.font.color.rgb = COLOR_WHITE
            _set_ppt_font(p_bar)

            tb = slide.shapes.add_textbox(
                col_x + Inches(0.2), top + Inches(0.6), col_w - Inches(0.4), col_h - Inches(0.7)
            )
            tf = tb.text_frame
            tf.word_wrap = True

            first = True
            metrics = item.get("metrics", [])
            pros = item.get("pros", [])
            dense = (len(metrics) + len(pros)) > 4
            metric_size = Pt(10.5) if dense else Pt(12)
            pro_size = Pt(9.5) if dense else Pt(11)
            item_space = Pt(2) if dense else Pt(4)

            # 关键指标
            for k, v in metrics:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.text = f"{k}：{v}"
                p.font.size = metric_size
                p.font.bold = True
                p.font.color.rgb = COLOR_PRIMARY
                _set_ppt_font(p)
                p.space_after = item_space

            # 核心要点 / 优势
            for pro in pros:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.text = f"•  {pro}"
                p.font.size = pro_size
                p.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p)
                p.space_after = item_space

        return slide

    def add_content_slide(
        self,
        title: str,
        paragraphs: list[str] | None = None,
        bullets: list[str] | None = None,
    ) -> Any:
        """添加标准正文内容页。"""
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        content_box = slide.shapes.add_textbox(
            Inches(0.8), Inches(1.2), Inches(8.4), Inches(3.8)
        )
        tf = content_box.text_frame
        tf.word_wrap = True

        first = True
        if paragraphs:
            total_p_chars = sum(len(p) for p in paragraphs)
            p_size = Pt(11) if (total_p_chars > 180 or len(paragraphs) > 3) else Pt(13)
            p_space = Pt(6) if p_size < Pt(13) else Pt(10)
            for para in paragraphs:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.text = para
                p.font.size = p_size
                p.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p)
                p.space_after = p_space

        if bullets:
            total_b_chars = sum(len(b) for b in bullets)
            b_size = Pt(10.5) if (total_b_chars > 160 or len(bullets) > 4) else Pt(13)
            b_space = Pt(5) if b_size < Pt(13) else Pt(8)
            for bullet in bullets:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.text = f"•  {bullet}"
                p.font.size = b_size
                p.font.color.rgb = COLOR_TEXT
                _set_ppt_font(p)
                p.space_after = b_space

        return slide

    def add_cards_slide(
        self,
        title: str,
        cards: list[dict[str, Any]],
        subtitle: str = "",
    ) -> Any:
        """添加多卡片统计页（对齐模板 Slide 10 的多色模块样式）。"""
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        if subtitle:
            sub_box = slide.shapes.add_textbox(
                Inches(0.85), Inches(0.78), Inches(8.3), Inches(0.32)
            )
            sp = sub_box.text_frame.paragraphs[0]
            sp.text = subtitle
            sp.font.size = Pt(10.5)
            sp.font.color.rgb = COLOR_MUTED
            _set_ppt_font(sp)

        n = len(cards)
        if n == 0:
            return slide

        left_margin = Inches(0.85)
        top_start = Inches(1.20)
        total_width = Inches(8.30)
        cols = min(n, 4)
        gap = Inches(0.20)
        card_w = (total_width - gap * (cols - 1)) / cols
        card_h = Inches(3.50) if n <= 4 else Inches(1.70)
        header_h = Inches(0.46)

        for i, card in enumerate(cards):
            r = i // cols
            c = i % cols
            x = left_margin + c * (card_w + gap)
            y = top_start + r * (card_h + Inches(0.20))
            theme_color = PALETTE[i % len(PALETTE)]

            # 1. 卡片主体浅灰底框 (对齐 Slide 10 #F7F9FC + 细边框)
            bg_rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y + header_h, card_w, card_h - header_h)
            bg_rect.fill.solid()
            bg_rect.fill.fore_color.rgb = COLOR_CARD_BG
            bg_rect.line.color.rgb = COLOR_BORDER
            bg_rect.line.width = Pt(1)

            # 2. 顶部实体色块标题栏（对齐 Slide 10 多色模块，白字加粗）
            header_rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, card_w, header_h)
            header_rect.fill.solid()
            header_rect.fill.fore_color.rgb = theme_color
            header_rect.line.fill.background()
            p_head = header_rect.text_frame.paragraphs[0]
            title_text = card.get("title", "")
            p_head.text = title_text
            p_head.font.size = Pt(12) if len(title_text) > 6 else Pt(13)
            p_head.font.bold = True
            p_head.font.color.rgb = COLOR_WHITE
            p_head.alignment = PP_ALIGN.CENTER
            _set_ppt_font(p_head)
            header_rect.text_frame.margin_left = header_rect.text_frame.margin_right = 0
            header_rect.text_frame.margin_top = header_rect.text_frame.margin_bottom = 0

            # 3. 内部指标数值与说明文本
            tb = slide.shapes.add_textbox(
                x + Inches(0.12), y + header_h + Inches(0.20), card_w - Inches(0.24), card_h - header_h - Inches(0.30)
            )
            tf = tb.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

            val = card.get("value")
            if val:
                p_val = tf.paragraphs[0]
                p_val.text = str(val)
                val_str = str(val)
                if len(val_str) > 10 or n > 4:
                    val_size = Pt(15)
                elif len(val_str) > 7:
                    val_size = Pt(18)
                else:
                    val_size = Pt(22)
                p_val.font.size = val_size
                p_val.font.bold = True
                p_val.font.color.rgb = COLOR_PRIMARY
                p_val.alignment = PP_ALIGN.CENTER
                _set_ppt_font(p_val)
                p_val.space_after = Pt(8)

            desc = card.get("desc")
            if desc:
                p_desc = tf.add_paragraph() if val else tf.paragraphs[0]
                p_desc.text = str(desc)
                desc_len = len(str(desc))
                desc_size = Pt(8.5) if desc_len > 30 else Pt(9.5)
                p_desc.font.size = desc_size
                p_desc.font.color.rgb = COLOR_TEXT
                p_desc.alignment = PP_ALIGN.CENTER
                _set_ppt_font(p_desc)

        return slide

    def add_table_slide(
        self,
        title: str,
        headers: list[str],
        rows: list[list[Any]],
        col_widths: list[float] | None = None,
    ) -> Any:
        """添加标准数据表格页（对齐模板 Slide 9 表格样式 1）。"""
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        num_rows = len(rows) + 1
        num_cols = len(headers)

        table_shape = slide.shapes.add_table(
            num_rows, num_cols, Inches(0.85), Inches(1.15), Inches(8.30), Inches(3.60)
        )
        tbl = table_shape.table

        if col_widths and len(col_widths) == num_cols:
            for idx, w in enumerate(col_widths):
                tbl.columns[idx].width = Inches(w)

        for col_idx, header in enumerate(headers):
            cell = tbl.cell(0, col_idx)
            cell.text = str(header)
            cell.fill.solid()
            cell.fill.fore_color.rgb = COLOR_PRIMARY
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                p.font.size = Pt(11)
                p.font.bold = True
                p.font.color.rgb = COLOR_WHITE
                _set_ppt_font(p)

        for row_idx, row_data in enumerate(rows, start=1):
            bg = COLOR_CARD_BG if row_idx % 2 == 1 else COLOR_WHITE
            for col_idx, val in enumerate(row_data):
                cell = tbl.cell(row_idx, col_idx)
                cell.text = str(val if val is not None else "-")
                cell.fill.solid()
                cell.fill.fore_color.rgb = bg
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                for p in cell.text_frame.paragraphs:
                    p.alignment = PP_ALIGN.CENTER
                    p.font.size = Pt(10)
                    p.font.color.rgb = COLOR_TEXT
                    _set_ppt_font(p)

        return slide

    def save(self, output_path: str | Path) -> Path:
        """保存 PPT 文件，自动将官方封底移动至最后（幂等安全，绝不误移目录页）。"""
        if self._closing_sld_id is not None and self._closing_sld_id in self.prs.slides._sldIdLst:
            if self.prs.slides._sldIdLst[-1] != self._closing_sld_id:
                self.prs.slides._sldIdLst.remove(self._closing_sld_id)
                self.prs.slides._sldIdLst.append(self._closing_sld_id)

        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(out))
        return out

    def to_pdf(self, output_pdf_path: str | Path | None = None) -> Path:
        """保存 PPT 并直接转换为高保真 PDF (P0)，供 pdf_preview 渲染目检或交付。"""
        if output_pdf_path is None:
            temp_pptx = Path("data/uploads/outbound/temp_presentation.pptx").resolve()
            out_pdf = temp_pptx.with_suffix(".pdf")
        else:
            out_pdf = Path(output_pdf_path).resolve()
            temp_pptx = out_pdf.with_suffix(".pptx")

        self.save(temp_pptx)
        return convert_pptx_to_pdf(temp_pptx, out_pdf)
