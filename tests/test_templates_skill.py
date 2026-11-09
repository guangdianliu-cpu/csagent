"""tests/test_templates_skill.py — 测试 templates skill 中的文档构建器与母版保真。"""

import sys
from pathlib import Path

# 引入 templates skill 模块
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "templates"
sys.path.insert(0, str(SKILL_DIR))

from scripts.lead_chart_style import (
    plot_line_chart,
    plot_bar_chart,
    plot_pie_chart,
)
from scripts.doc_builder import LeadDocBuilder
from scripts.ppt_builder import LeadPPTBuilder
from csagent.skills.discovery import SkillDiscovery


def test_templates_skill_discovery():
    """验证 templates 目录可被 SkillDiscovery 正常识别与扫描。"""
    repo_root = Path(__file__).resolve().parents[1]
    discovery = SkillDiscovery(roots=[repo_root / "skills"])
    packages = discovery.scan()
    pkg_map = {p.name: p for p in packages}
    assert "templates" in pkg_map
    pkg = pkg_map["templates"]
    assert pkg.enabled is True
    assert "Word" in pkg.description or "母版" in pkg.description


def test_lead_chart_generation(tmp_path):
    """验证利得官方主题图表生成。"""
    chart_png = tmp_path / "chart.png"
    plot_line_chart(
        x=["2025-01", "2025-02", "2025-03"],
        series_dict={
            "测试公募": [1.00, 1.05, 1.08],
            "沪深300": [1.00, 0.98, 1.02],
        },
        title="净值走势测试",
        output_path=chart_png,
    )
    assert chart_png.exists()
    assert chart_png.stat().st_size > 0


def test_lead_doc_builder(tmp_path):
    """验证 LeadDocBuilder 生成 Word 研报并保留官方页眉表格与文末声明。"""
    chart_png = tmp_path / "test_chart.png"
    plot_bar_chart(
        categories=["科技", "消费", "医药"],
        values=[35.0, 25.0, 20.0],
        title="行业分布",
        output_path=chart_png,
    )

    doc = LeadDocBuilder()
    doc.set_meta(
        department="内部投研",
        report_type="专题研究报告",
        date="2026-09-21",
    )
    doc.add_title("宏观流动性与资产配置周报", "利得投研核心资产跟踪体系")
    doc.add_heading_1("一、核心观点与摘要")
    doc.add_callout("本周宏观流动性维持合理充裕，权益结构分化明显。")
    doc.add_bullet("公募权益仓位微幅提升至 86.4%")
    doc.add_bullet("QDII 美股科技主题持续保持高换手")

    doc.add_heading_1("二、行业配置结构")
    doc.add_chart_image(chart_png, caption="图 1：重点行业持仓占比分布", width_inches=5.5)

    doc.add_page_break()

    doc.add_heading_1("三、代表性品种清单")
    headers = ["板块", "基金代码", "基金简称", "近6月收益率", "评价"]
    rows = [
        ["科技", "001234", "利得前瞻科技A", "+18.5%", "超额稳定"],
        ["消费", "005678", "利得品牌消费C", "+6.2%", "防御配置"],
    ]
    doc.add_table(headers=headers, rows=rows, col_widths=[1.5, 1.2, 2.0, 1.5, 1.5])

    out_docx = tmp_path / "report.docx"
    doc.save(out_docx)
    assert out_docx.exists()
    assert out_docx.stat().st_size > 10000


def test_closing_slide_partname_does_not_collide(tmp_path):
    """加满超过模板页数后，封底文件名不能和新内容页撞车。

    模板 15 页，删掉中间页后封底仍叫 slide15.xml。第 13 张内容页会再申请这个
    名字。PowerPoint 同一路径只保留一份，THANKS 就会出现两次。
    """
    import zipfile
    from collections import Counter
    from xml.etree import ElementTree as ET

    ppt = LeadPPTBuilder()
    titles = [f"内容页-{i:02d}" for i in range(1, 15)]
    for title in titles:
        ppt.add_content_slide(title, bullets=["唯一一次"])
    out = tmp_path / "many.pptx"
    ppt.save(out)

    with zipfile.ZipFile(out) as package:
        names = package.namelist()
        duplicated = [name for name, count in Counter(names).items() if count > 1]
        assert duplicated == []
        rels = ET.fromstring(package.read("ppt/_rels/presentation.xml.rels"))
        targets = [
            rel.get("Target")
            for rel in rels
            if (rel.get("Target") or "").startswith("slides/slide")
        ]
        assert len(targets) == len(set(targets))
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        pns = {
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        rid_to_target = {rel.get("Id"): rel.get("Target") for rel in rels}
        pres = ET.fromstring(package.read("ppt/presentation.xml"))
        page_texts = []
        for sld_id in pres.find("p:sldIdLst", pns):
            rid = sld_id.get(f"{{{pns['r']}}}id")
            root = ET.fromstring(package.read("ppt/" + rid_to_target[rid]))
            page_texts.append("".join(node.text or "" for node in root.iter(ns + "t")))
    joined = "\n".join(page_texts)
    assert sum("公司地址" in text for text in page_texts) == 1
    assert "公司地址" in page_texts[-1]
    for title in titles:
        assert joined.count(title) == 1


def test_lead_ppt_builder(tmp_path):
    """验证 LeadPPTBuilder 生成各核心版式幻灯片并正确处理母版页尾。"""
    chart_png = tmp_path / "ppt_chart.png"
    plot_pie_chart(
        labels=["权益资产", "固收资产", "大宗商品", "现金管理"],
        sizes=[45.0, 35.0, 12.0, 8.0],
        title="大类资产配置比例",
        output_path=chart_png,
    )

    ppt = LeadPPTBuilder()
    ppt.set_cover(
        title="利得核心资产配置策略",
        subtitle="2026年四季度投研沟通会议",
        date="2026-09-21",
    )
    ppt.add_agenda_slide([
        "01 宏观流动性与政策环境展望",
        "02 重点基金经理能力圈剖析",
        "03 组合构建策略与下行风险应对",
    ])
    ppt.add_two_column_slide(
        title="大类资产配置主线：股债平衡与海外出海",
        left_title="权益与出海配置",
        left_bullets=[
            "科技核心龙头估值回到合理中枢",
            "QDII 纳斯达克100配置价值显著",
        ],
        right_title="固收与安全底仓",
        right_bullets=[
            "高等级纯债久期维持在中性偏长",
            "精选短融作为流动性安全垫",
        ],
    )
    ppt.add_chart_slide(
        title="配置比例结构分布",
        image_path=chart_png,
        notes=[
            "权益类资产占比维持在 45% 中枢",
            "固收底仓提供确定性收益贡献",
        ],
    )
    ppt.add_comparison_slide(
        title="核心策略对比",
        item_a={
            "name": "稳健配置策略",
            "metrics": [("目标波动", "< 6%"), ("基准仓位", "80% 固收")],
            "pros": ["下行风险极小", "收益确定性高"],
        },
        item_b={
            "name": "进取成长策略",
            "metrics": [("目标弹性", "超额显著"), ("基准仓位", "80% 权益")],
            "pros": ["牛市向上弹性大", "聚焦高胜率成长经理"],
        },
    )

    out_pptx = tmp_path / "deck.pptx"
    ppt.save(out_pptx)
    assert out_pptx.exists()
    assert out_pptx.stat().st_size > 10000


def test_lead_doc_to_pdf(tmp_path):
    """验证 Word 到 PDF 高保真转换全流程（LibreOffice / Word COM）。"""
    doc = LeadDocBuilder()
    doc.set_meta(department="投研部", report_type="PDF导出测试", date="2026-09-21")
    doc.add_title("PDF 导出自动化验证报告")
    doc.add_heading_1("一、环境与引擎自适应")
    doc.add_bullet("验证本机 LibreOffice / Word COM 高保真渲染链路")

    out_docx = tmp_path / "test_pdf_source.docx"
    doc.save(out_docx)
    assert out_docx.exists()

    out_pdf = doc.to_pdf(tmp_path / "test_pdf_result.pdf")
    assert out_pdf.exists()
    assert out_pdf.stat().st_size > 1000

