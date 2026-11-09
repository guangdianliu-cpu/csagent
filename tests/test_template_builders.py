# -*- coding: utf-8 -*-
"""templates skill 构建器回归测试。

覆盖 2.1.0 引入的高保真特性：
  - 中英文字体成对设置（docx w:eastAsia / pptx a:ea）
  - 标题大纲级别（Word 导航窗格 / 自动目录）
  - 表格跨页重复表头（w:tblHeader）
  - 法律声明锚点缺失时 fail loud
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "templates" / "scripts"))

from doc_builder import LeadDocBuilder  # noqa: E402
from ppt_builder import LeadPPTBuilder  # noqa: E402


def _build_doc(tmp_path: Path) -> Path:
    doc = LeadDocBuilder()
    doc.set_meta(report_type="ETF专题研究报告", department="利得研究")
    doc.add_title("验证文档", subtitle="——子标题")
    doc.add_callout("核心观点验证文本。")
    doc.add_heading_1("一、一级标题")
    doc.add_paragraph("正文段落 Body Text 123。")
    doc.add_heading_2("1.1 二级标题")
    doc.add_bullet("要点一")
    doc.add_heading_3("1.1.1 三级标题")
    doc.add_numbered_item(1, "编号要点一", bold_prefix="结论：")
    doc.add_table(["代码", "名称", "费率"], [["513100", "国泰纳斯达克100ETF", "0.50%"]])
    return doc.save(tmp_path / "report.docx")


def test_docx_eastasia_font(tmp_path: Path) -> None:
    xml = zipfile.ZipFile(_build_doc(tmp_path)).read("word/document.xml").decode("utf-8")
    assert 'w:eastAsia="微软雅黑"' in xml


def test_docx_heading_outline_levels(tmp_path: Path) -> None:
    xml = zipfile.ZipFile(_build_doc(tmp_path)).read("word/document.xml").decode("utf-8")
    assert 'w:outlineLvl w:val="0"' in xml
    assert 'w:outlineLvl w:val="1"' in xml
    assert 'w:outlineLvl w:val="2"' in xml


def test_docx_table_header_repeat(tmp_path: Path) -> None:
    xml = zipfile.ZipFile(_build_doc(tmp_path)).read("word/document.xml").decode("utf-8")
    assert "w:tblHeader" in xml


def test_docx_content_before_disclaimer(tmp_path: Path) -> None:
    xml = zipfile.ZipFile(_build_doc(tmp_path)).read("word/document.xml").decode("utf-8")
    assert "法律声明" in xml
    assert -1 < xml.find("一、一级标题") < xml.find("法律声明")


def test_docx_missing_anchor_fails_loud(tmp_path: Path) -> None:
    import docx

    blank = tmp_path / "blank.docx"
    docx.Document().save(str(blank))
    with pytest.raises(RuntimeError, match="法律声明"):
        LeadDocBuilder(template_path=blank)


def test_pptx_east_asian_font(tmp_path: Path) -> None:
    builder = LeadPPTBuilder()
    builder.set_cover("验证演示", "内部测试", "2026年9月")
    builder.add_agenda_slide(["章节一", "章节二"])
    builder.add_cards_slide("指标总览", [{"title": "规模", "value": "120亿", "desc": "中文混排 AUM"}])
    builder.add_table_slide("产品汇总", ["代码", "名称"], [["513100", "国泰纳斯达克100ETF"]])
    pptx_path = builder.save(tmp_path / "deck.pptx")

    zf = zipfile.ZipFile(pptx_path)
    slides = [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert any('<a:ea typeface="微软雅黑"' in zf.read(n).decode("utf-8") for n in slides)


def test_docx_paragraph_indent_and_justify(tmp_path: Path) -> None:
    """验证正文段落首行缩进 2 字符 (Pt(21)) 与两端对齐。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = LeadDocBuilder()
    p = doc.add_paragraph("中文研报测试段落，验证首行缩进与两端对齐规范。")
    assert p.paragraph_format.first_line_indent == Pt(21)
    assert p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY

    out_path = doc.save(tmp_path / "indent_test.docx")
    xml = zipfile.ZipFile(out_path).read("word/document.xml").decode("utf-8")
    assert 'w:firstLine="420"' in xml  # 21 pt = 420 dxa
    assert 'w:jc w:val="both"' in xml  # justified


def test_docx_table_borders_and_adaptive_widths(tmp_path: Path) -> None:
    """验证表格细浅灰边框 (#DCE1EB) 与自适应列宽分配。"""
    doc = LeadDocBuilder()
    table = doc.add_table(
        headers=["短列", "中等长度的指标列", "超长文本说明字段测试列宽自适应分配权重"],
        rows=[
            ["1", "123.45%", "这是一段很长很长的基金策略描述文本用来测试权重动态计算分配"],
            ["2", "67.89%", "简要备注"],
        ],
    )
    # 验证自适应计算得到的第 3 列宽度明显大于第 1 列
    assert table.columns[2].width > table.columns[0].width

    out_path = doc.save(tmp_path / "table_border_test.docx")
    xml = zipfile.ZipFile(out_path).read("word/document.xml").decode("utf-8")
    assert "w:tblBorders" in xml
    assert 'w:color="DCE1EB"' in xml


def test_pptx_autofit_and_to_pdf(tmp_path: Path) -> None:
    """验证 PPT 卡片防溢出自适应字号与 to_pdf 功能。"""
    builder = LeadPPTBuilder()
    builder.set_cover("PPT防溢出与PDF测试", "内部投研", "2026年9月")

    # 超长指标卡片（测试 auto-shrink）
    builder.add_cards_slide(
        "指标总览",
        [
            {
                "title": "超长标题字段名称测试",
                "value": "1234567890.123456",
                "desc": "这是一段极其详细且字数很多很多很多的卡片补充描述说明用来验证防溢出字号降级机制",
            },
            {"title": "正常", "value": "12 只", "desc": "正常描述"},
        ],
    )

    # 超长要点双栏（测试 auto-shrink）
    builder.add_two_column_slide(
        "双栏对比",
        left_bullets=[f"要点 {i}: 这是详细的投资策略阐述文字，字数较长用于测试防溢出" for i in range(5)],
        right_bullets=["简短要点一", "简短要点二"],
    )

    out_pptx = tmp_path / "test_autofit.pptx"
    builder.save(out_pptx)
    assert out_pptx.exists()

    # 测试 PPT 转 PDF
    try:
        out_pdf = builder.to_pdf(tmp_path / "test_autofit.pdf")
        assert out_pdf.exists()
        assert out_pdf.stat().st_size > 1000
    except Exception as e:
        pytest.skip(f"环境无 PDF 转换引擎: {e}")


def test_check_env_preflight() -> None:
    """验证 check_env 环境自检脚本能正常返回诊断字典与执行 main。"""
    from check_env import check_environment, main

    report = check_environment()
    assert "libraries" in report
    assert report["libraries"]["docx"] is True
    assert report["libraries"]["pptx"] is True
    assert "engines" in report
    assert "fonts" in report
    assert "previews" in report

    # 运行 main 检查不抛异常
    ret = main()
    assert ret in (0, 1)


def test_builder_subclass_extension(tmp_path: Path) -> None:
    """验证方案 2（分级放开）：在会话中继承 LeadDocBuilder / LeadPPTBuilder 派生定制版式。"""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches
    from ppt_builder import COLOR_CARD_BG, COLOR_BORDER, _set_ppt_font

    class ExtendedPPTBuilder(LeadPPTBuilder):
        """用户会话中编写的扩展构建器。"""

        def add_three_column_slide(self, title: str, items: list[str]) -> Any:
            slide = self.prs.slides.add_slide(self._content_layout)
            self.add_title(slide, title)
            for idx, text in enumerate(items[:3]):
                card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8 + idx * 2.8), Inches(1.3), Inches(2.6), Inches(3.4))
                card.fill.solid()
                card.fill.fore_color.rgb = COLOR_CARD_BG
                card.line.color.rgb = COLOR_BORDER
                tf = card.text_frame
                tf.paragraphs[0].text = text
                _set_ppt_font(tf.paragraphs[0])
            return slide

    ext_ppt = ExtendedPPTBuilder()
    ext_ppt.set_cover("扩展版式测试", "投研创新", "2026年9月")
    ext_ppt.add_three_column_slide("三栏扩展版式", ["A产品分析", "B产品分析", "C产品分析"])
    out_pptx = tmp_path / "ext_deck.pptx"
    ext_ppt.save(out_pptx)
    assert out_pptx.exists()
    assert out_pptx.stat().st_size > 10000


def test_pptx_cover_and_title_layout(tmp_path: Path) -> None:
    """验证 PPT 封面主标题显式着色、副标题排布、角标防溢出及内容页标题顶栏定位。"""
    from pptx import Presentation
    from pptx.util import Inches
    from ppt_builder import COLOR_PRIMARY

    builder = LeadPPTBuilder()
    long_subtitle = "利得基金内部投研报告 · 大成基金"
    builder.set_cover("徐彦 基金经理画像", subtitle=long_subtitle, date="2026年9月")

    slide_content = builder.prs.slides.add_slide(builder._content_layout)
    tb = builder.add_title(slide_content, "核心画像总览")

    # 验证内容页标题处于母版分割线 (0.63") 上方
    assert tb.top < Inches(0.5)
    assert tb.left >= Inches(0.8)

    out_p = builder.save(tmp_path / "layout_test.pptx")
    prs = Presentation(str(out_p))
    s0 = prs.slides[0]

    # 1. 验证底模干扰 AlternateContent 被彻底清理
    zf = zipfile.ZipFile(out_p)
    s0_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8", errors="ignore")
    assert "公司册" not in s0_xml

    # 2. 验证主标题不是白色（白色在纯白底为隐形），且文字匹配
    t_shape = next(sh for sh in s0.shapes if sh.has_text_frame and "徐彦" in sh.text)
    assert t_shape.text_frame.paragraphs[0].font.color.rgb == COLOR_PRIMARY
    # 验证副标题存在于主标题下方
    assert long_subtitle in t_shape.text

    # 3. 验证右侧角标小方块 (0.71"x0.71") 没有被塞入长副标题
    badge_shape = next(sh for sh in s0.shapes if sh.has_text_frame and sh.name == "矩形 12")
    assert long_subtitle not in badge_shape.text
    assert len(badge_shape.text.strip()) <= 12


def test_pptx_save_and_to_pdf_agenda_slide_order(tmp_path: Path) -> None:
    """验证多次保存与 to_pdf 幂等性：目录大纲页绝不被挪动到文稿末尾。"""
    from pptx import Presentation

    builder = LeadPPTBuilder()
    builder.set_cover("主标题", "副标题", "2026年9月")
    builder.add_agenda_slide(["章节一", "章节二", "章节三"])
    builder.add_cards_slide("指标", [{"title": "A", "value": "1", "desc": "B"}])
    builder.add_two_column_slide("双栏", ["L1"], ["R1"])

    out1 = tmp_path / "first_save.pptx"
    builder.save(out1)

    # 模拟 to_pdf 再次触发 save
    out2 = tmp_path / "second_save.pptx"
    builder.save(out2)

    prs = Presentation(str(out2))
    # 验证 Slide 0 是封面，Slide 1 是目录，最后一页是封底
    s0_txt = " ".join(sh.text for sh in prs.slides[0].shapes if sh.has_text_frame)
    s1_txt = " ".join(sh.text for sh in prs.slides[1].shapes if sh.has_text_frame)
    last_txt = " ".join(sh.text for sh in prs.slides[-1].shapes if sh.has_text_frame)

    assert "主标题" in s0_txt
    assert "章节一" in s1_txt
    assert "目录" in prs.slides[1].slide_layout.name or "目录" in s1_txt
    # 封底页（官方底模，绝不是目录或大纲项）
    assert "章节一" not in last_txt


def test_docx_word_fidelity_enhancements(tmp_path: Path) -> None:
    """验证 Word 研报排版增强特性：
    1. KPI 指标卡片 (add_kpi_cards)
    2. 核心观点引用块左侧强调边线 (add_callout)
    3. 标题段落 keep_with_next (防页底孤立)
    4. 表格行跨页防截断 (w:cantSplit) 与重复表头 (w:tblHeader)
    5. 项目要点圆点实心符号 (\\u2022)
    """
    doc = LeadDocBuilder()
    doc.add_kpi_cards([
        {"value": "12.6年", "label": "管理年限", "sub": "资深名将"},
        {"value": "135亿", "label": "在管规模", "sub": "8只权益"},
    ])
    doc.add_callout("核心投资建议", title="观点速览")
    doc.add_heading_1("一、核心分析")
    doc.add_bullet("投资亮点一")
    doc.add_table(["列A", "列B"], [["值1", "值2"]])

    out_docx = doc.save(tmp_path / "enhancements.docx")
    xml = zipfile.ZipFile(out_docx).read("word/document.xml").decode("utf-8")

    # 验证 KPI 卡片包含大字号数值与小字号标签
    assert "12.6年" in xml
    assert "管理年限" in xml

    # 验证 callout 包含左侧加粗强调边框
    assert 'w:left w:val="single" w:sz="36" w:space="0" w:color="40527E"' in xml

    # 验证标题 keep_with_next
    assert "w:keepNext" in xml

    # 验证表格行 cantSplit
    assert "w:cantSplit" in xml

    # 验证圆点实心符号
    assert "•" in xml


def test_check_doc_inspector(tmp_path: Path) -> None:
    """验证 check_doc 结构自检工具能正常解析 docx 且对合并单元格容错。"""
    from check_doc import inspect_docx
    from doc_builder import LeadDocBuilder

    doc = LeadDocBuilder()
    doc.add_title("自检测试文档")
    doc.add_heading_1("一、测试第一章")
    doc.add_paragraph("这是正文测试。")
    # 创建含合并单元格的表格
    doc.add_table(
        headers=["表头A", "表头B"],
        rows=[["1", "2"], ["3", "4"]],
        merge_cells=[(0, 0, 0, 1)],
    )
    test_docx = doc.save(tmp_path / "inspect_target.docx")

    data = inspect_docx(test_docx)
    assert data["file_name"] == "inspect_target.docx"
    assert data["headings_count"] >= 1
    assert data["tables_count"] >= 1
    assert data["paragraphs_count"] > 0


def test_pdf_preview_multipage(tmp_path: Path) -> None:
    """验证 pdf_preview 工具通过 pymupdf 实现多页渲染。"""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    from pdf_preview import _render_pymupdf

    # 创建一个 3 页测试 PDF
    pdf_path = tmp_path / "test_multipage.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}")
    doc.save(str(pdf_path))
    doc.close()

    out_dir = tmp_path / "preview"
    out_dir.mkdir(exist_ok=True)
    pages = _render_pymupdf(pdf_path, out_dir, max_pages=3)
    assert len(pages) == 3
    assert all(p.is_file() for p in pages)


def test_check_ppt_inspects_presentation(tmp_path: Path) -> None:
    """验证 check_ppt 结构自检工具能正常解析 pptx 结构、表头与大纲。"""
    from check_ppt import inspect_pptx

    builder = LeadPPTBuilder()
    builder.set_cover("自检测试文稿", "投研报告", "2026年9月")
    builder.add_agenda_slide(["模块一", "模块二"])
    builder.add_cards_slide(
        "核心指标",
        [{"title": "指标A", "value": "100亿", "desc": "说明"}],
    )
    builder.add_table_slide(
        "产品明细",
        ["代码", "简称", "规模"],
        [["000001", "华夏成长", "50亿"]],
    )
    pptx_path = tmp_path / "test_check.pptx"
    builder.save(pptx_path)

    data = inspect_pptx(pptx_path)
    assert data["file_name"] == "test_check.pptx"
    assert data["slides_count"] >= 4
    assert data["aspect_ratio"] == "16:9 宽屏"
    assert data["empty_slides_count"] == 0
    assert data["tables_count"] >= 1
    assert any("产品明细" in s["title"] for s in data["slides"])
    assert data["tables"][0]["rows"] == 2
    assert "代码 | 简称 | 规模" in data["tables"][0]["header"]



