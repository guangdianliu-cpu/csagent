"""报告渲染与导出：markdown 小转换器、SVG 图表、Excel、PDF 缓存。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openpyxl import load_workbook

from csagent.channels import workspace_uploads as wu
from csagent.reports import export, render, store
from csagent.reports.model import ChartBlock, ReportContent
from csagent.reports.render import chart_svg, markdown_to_html, report_html, signed_class

SID = "web:u1"


@pytest.fixture(autouse=True)
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(wu, "DEFAULT_UPLOADS_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(wu, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _report(blocks=None):
    content = ReportContent.model_validate(
        {
            "title": "A/B 对比:<测试>",
            "summary": "摘要",
            "blocks": blocks
            or [
                {"type": "markdown", "text": "## 小节\n- 一"},
                {
                    "type": "table",
                    "title": "核心指标",
                    "columns": ["指标", "A", "B"],
                    "rows": [["近一年", "-3.2%", "+1.5%"], ["回撤", -12.0, None]],
                    "signed_columns": ["A", "B"],
                },
                {"type": "chart", "chart": "bar", "x": ["近一年"], "series": [{"name": "A", "data": [-3.2]}]},
                {"type": "conclusion", "text": "结论"},
            ],
            "sources": ["测试库"],
        }
    )
    report, _ = store.save(SID, content)
    return report


class TestMarkdown:
    def test_structure(self):
        out = markdown_to_html("# 标题\n\n正文 **粗** 和 `code`\n\n- 一\n  - 一.1\n- 二\n\n1. 甲\n2. 乙\n\n> 注\n\n---")
        assert "<h1>标题</h1>" in out
        assert "<strong>粗</strong>" in out and "<code>code</code>" in out
        assert out.count("<ul>") == 2 and "<ol>" in out
        assert "<blockquote>" in out and "<hr>" in out

    def test_table_and_pct(self):
        out = markdown_to_html("| 名称 | 收益 |\n|---|---:|\n| A | -3.2% |\n| B | +1.5% |")
        assert "<table>" in out and "<th>名称</th>" in out
        assert '<span class="num-down">-3.2%</span>' in out
        assert '<span class="num-up">+1.5%</span>' in out

    def test_escapes_html_and_bad_links(self):
        out = markdown_to_html("<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.b/c)")
        assert "<script>" not in out
        assert 'href="javascript' not in out
        assert '<a href="https://a.b/c">ok</a>' in out

    def test_code_fence_untouched(self):
        out = markdown_to_html("```\n-3% **x**\n```")
        assert "<pre><code>-3% **x**</code></pre>" in out


@pytest.mark.parametrize(
    "value,cls",
    [("-3.2%", "num-down"), ("+1.5%", "num-up"), ("1.5", "num-up"), ("0.00%", ""), (-1, "num-down"), (None, ""), ("—", "")],
)
def test_signed_class(value, cls):
    assert signed_class(value) == cls


class TestChart:
    @pytest.mark.parametrize("kind,mark", [("line", "<polyline"), ("bar", "<rect"), ("pie", "<path")])
    def test_kinds(self, kind, mark):
        block = ChartBlock.model_validate(
            {"type": "chart", "chart": kind, "title": "图名不进画布", "x": ["<b>1月", "2月", "3月"],
             "series": [{"name": "A", "data": [1.0, None, 3.0]}]}
        )
        svg = chart_svg(block)
        assert svg.startswith("<svg")
        assert mark in svg
        assert "<b>" not in svg and "&lt;b&gt;" in svg
        assert "图名不进画布" not in svg
        # 报告页交互用的感应区：折线 / 柱状每个 x 一块，饼图每个扇区带下标
        hit = "c-slice" if kind == "pie" else "c-hit"
        assert f'class="{hit}" data-i="0"' in svg

    def test_hover_marks(self):
        """折线每个 x 一组隐藏圆点、柱子带下标，报告页悬停时点亮 / 其余变淡。"""
        def svg(kind):
            return chart_svg(ChartBlock.model_validate(
                {"type": "chart", "chart": kind, "x": ["a", "b"], "series": [{"name": "A", "data": [1, 2]}]}
            ))
        assert svg("line").count('class="c-dots"') == 2 and 'visibility="hidden"' in svg("line")
        assert 'class="c-bar" data-i="1"' in svg("bar")

    def test_unit_label(self):
        block = ChartBlock.model_validate(
            {"type": "chart", "chart": "bar", "unit": "%", "x": ["a"], "series": [{"name": "A", "data": [1]}]}
        )
        assert "单位：%" in chart_svg(block)

    def test_flat_series(self):
        block = ChartBlock.model_validate(
            {"type": "chart", "chart": "line", "x": ["a", "b"], "series": [{"name": "A", "data": [0, 0]}]}
        )
        assert "<polyline" in chart_svg(block)


def test_report_html_legacy_report():
    out = report_html(_report())
    assert "A/B 对比:&lt;测试&gt;" in out
    assert '<td class="num-down">-3.2%</td>' in out
    assert "<svg" in out and "conclusion" in out and "数据来源：测试库" in out
    # 旧报告没有章节标题，不出「一、」
    assert '<h2 class="sec-title">' not in out


def _sectioned():
    def sec(title, blocks):
        return {"title": title, "blocks": blocks}

    table = {"type": "table", "title": "指标", "columns": ["a"], "rows": [["1"]], "source": "库A"}
    chart = {"type": "chart", "chart": "line", "title": "走势", "x": ["1"], "series": [{"name": "A", "data": [1]}]}
    content = ReportContent.model_validate(
        {
            "title": "报告",
            "summary": "摘要",
            "as_of": "2026-10-28",
            "key_points": ["观点一 +3.2%", "观点二"],
            "sections": [
                sec("概览", [{"type": "markdown", "text": "正文"}, table]),
                sec("分项", [chart, table]),
                sec("风险", [chart]),
            ],
            "risk_note": "样本仅 3 个月",
            "sources": ["库A"],
        }
    )
    return store.save(SID, content)[0]


def test_numbering_and_skeleton():
    report = _sectioned()
    labels = [[label for _, label in items] for _, items in render.numbered(report)]
    assert labels == [["", "表 1"], ["图 1", "表 2"], ["图 2"]]
    out = report_html(report, risk_note_default="通用免责", footer="内部资料")
    order = ["核心观点", "一、概览", "二、分项", "三、风险", "风险提示", "数据来源"]
    positions = [out.index(x) for x in order]
    assert positions == sorted(positions)
    # 三章不出目录（render.TOC_MIN = 4）
    assert '<div class="box-title">目录</div>' not in out
    assert "<b>表 2</b>指标" in out and "<b>图 2</b>走势" in out
    assert "资料来源：库A" in out
    assert "数据截至 2026-10-28" in out
    assert "样本仅 3 个月" in out and "通用免责" in out
    assert '"内部资料"' in out and "counter(pages)" in out
    assert '<span class="num-up">+3.2%</span>' in out


def test_xlsx_sheets_follow_table_numbers():
    path = asyncio.run(export.export(SID, _sectioned(), "xlsx"))
    wb = load_workbook(path)
    assert wb.sheetnames == ["表1 指标", "表2 指标", "说明"]
    info = {row[0].value: row[1].value for row in wb["说明"].iter_rows()}
    assert info["数据截至"] == "2026-10-28"
    assert info["核心观点 1"] == "观点一 +3.2%"


class TestXlsx:
    def test_sheets_numbers_and_colors(self, tmp_path):
        path = asyncio.run(export.export(SID, _report(), "xlsx"))
        assert path.name.endswith("_v1.xlsx")
        wb = load_workbook(path)
        assert wb.sheetnames == ["表1 核心指标", "说明"]
        ws = wb["表1 核心指标"]
        assert [c.value for c in ws[1]] == ["指标", "A", "B"]
        assert ws["B2"].value == pytest.approx(-0.032)
        assert ws["B2"].number_format == "0.00%"
        assert ws["B2"].font.color.rgb.endswith("48C993")
        assert ws["C2"].font.color.rgb.endswith("E85951")
        assert ws["B3"].value == -12.0

    def test_no_tables(self):
        report = _report([{"type": "markdown", "text": "只有文字"}])
        assert not export.has_tables(report)
        with pytest.raises(ValueError, match="没有表格"):
            asyncio.run(export.export(SID, report, "xlsx"))


def test_pdf_rendered_once_then_cached(monkeypatch):
    calls = []

    async def fake_render(html_path, pdf_path, **kwargs):
        calls.append(html_path.read_text(encoding="utf-8"))
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return "fake"

    monkeypatch.setattr(export, "render_html_to_pdf_async", fake_render)
    report = _report()
    first = asyncio.run(export.export(SID, report, "pdf"))
    second = asyncio.run(export.export(SID, report, "pdf"))
    assert first == second and first.suffix == ".pdf"
    assert len(calls) == 1 and "<svg" in calls[0]
    assert not first.with_suffix(".html").exists()


def test_unknown_format():
    with pytest.raises(ValueError):
        asyncio.run(export.export(SID, _report(), "docx"))


# ------------------------------------------------------------ P0/P1：报告头可视化、记分卡、散点、排名条


def _visual():
    content = ReportContent.model_validate(
        {
            "title": "三只对比",
            "summary": "乙最稳",
            "verdict": "稳健型首选乙",
            "kpis": [
                {"label": "最大回撤 · 乙", "value": "-6%", "note": "最小"},
                {"label": "夏普 · 乙", "value": "1.2", "tone": "neutral"},
            ],
            "key_points": [{"text": "乙三项最优", "tone": "plus"}, "纯文字观点"],
            "sections": [
                {
                    "title": "对比",
                    "lead": "乙风险最低",
                    "blocks": [
                        {
                            "type": "table",
                            "title": "记分卡",
                            "columns": ["指标", "甲", "乙", "丙"],
                            "rows": [["收益", "+8.2%", "+5.1%", "+12.4%"], ["回撤", "-11%", "-6%", "-19%"], ["规模", 32, 58, 15]],
                            "row_best": ["max", "max", None],
                        },
                        {"type": "chart", "chart": "scatter", "x_label": "回撤", "y_label": "收益",
                         "points": [{"name": "甲", "x": 11, "y": 8.2}, {"name": "乙", "x": 6, "y": 5.1}]},
                        {"type": "chart", "chart": "rank", "unit": "%", "x": ["甲", "乙", "丙"],
                         "series": [{"name": "收益", "data": [8.2, -2.0, 12.4]}]},
                    ],
                }
            ],
        }
    )
    return store.save(SID, content)[0]


def test_best_cells_rows_and_columns():
    report = _visual()
    table = report.sections[0].blocks[0]
    assert render.best_cells(table) == [[0, 3], [1, 2]]
    by_col = table.model_copy(update={"row_best": [], "col_best": {"乙": "min"}})
    assert render.best_cells(by_col) == [[1, 2]]


def test_visual_head_and_lead_in_pdf_html():
    out = report_html(_visual())
    order = ["结论", "稳健型首选乙", "最大回撤 · 乙", "核心观点", "一、对比", "乙风险最低"]
    positions = [out.index(x) for x in order]
    assert positions == sorted(positions)
    assert '<div class="kpi-value num-down">-6%</div>' in out
    assert '<div class="kpi-value ">1.2</div>' in out
    assert '<span class="tone tone-plus">优势</span>' in out
    assert out.count('<span class="best">') == 2
    assert '<span class="best"><span class="num-down">-6%</span></span>' in out


def test_rank_sorted_and_scatter_hits():
    blocks = _visual().sections[0].blocks
    scatter, rank = chart_svg(blocks[1]), chart_svg(blocks[2])
    assert scatter.count('class="c-hit"') == 2 and "回撤 →" in scatter
    # 从大到小：丙(2) 甲(0) 乙(1)；负数单独配色
    order = [int(m) for m in __import__("re").findall(r'class="c-bar" data-i="(\d)"', rank)]
    assert order == [2, 0, 1]
    assert render.RANK_DOWN in rank and "+12.4%" in rank


@pytest.mark.parametrize(
    "block,msg",
    [
        ({"type": "chart", "chart": "scatter", "points": [{"name": "a", "x": 1, "y": 1}]}, "至少 2 个点"),
        ({"type": "chart", "chart": "scatter", "points": [{"name": "a", "x": 1, "y": 1}, {"name": "b", "x": 2, "y": 2}]}, "x_label"),
        ({"type": "chart", "chart": "rank", "x": ["a"], "series": [{"name": "1", "data": [1]}, {"name": "2", "data": [2]}]}, "1 个系列"),
        ({"type": "table", "columns": ["a", "b"], "rows": [["x", 1]], "row_best": ["max", "min"]}, "row_best"),
    ],
)
def test_new_block_validation(block, msg):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=msg):
        ReportContent.model_validate({"title": "t", "summary": "s", "sections": [{"title": "a", "blocks": [block]}]})


# ------------------------------------------------------------------ 排版层与通用模块


def test_numbers_are_tidied_for_reading():
    assert render.tidy_text("规模 18894.0 亿元，另一只 3200 亿") == "规模 1.89 万亿元，另一只 3200 亿"
    assert render.tidy_text("合计 12,345亿") == "合计 1.23 万亿"
    assert render.display_cell(73.0) == "73"
    assert render.display_cell(18894.0) == "18,894"
    assert render.display_cell(1.25) == "1.25"
    assert render.display_cell("+8.2%") == "+8.2%"


def test_bold_lead_paragraph_becomes_claim_block():
    out = markdown_to_html("**乙更稳。** 最多只跌过 6%，夏普也最高。\n\n**短**：太短")
    assert '<div class="claim"><div class="claim-head">乙更稳。</div>' in out
    assert '<p class="claim-body">最多只跌过 6%' in out
    # 展开的话不到 8 个字：还是普通段落
    assert "<p><strong>短</strong>：太短</p>" in out


def _block(raw: dict):
    content = ReportContent.model_validate({"title": "t", "summary": "s", "sections": [{"title": "a", "blocks": [raw]}]})
    return content.sections[0].blocks[0]


DUEL = {
    "type": "entities",
    "items": [{"name": "甲", "kind": "混合", "tags": ["成长"]}, {"name": "乙"}],
    "metrics": [
        {"label": "近一年", "values": ["+8.2%", "+5.1%"], "best": "max"},
        {"label": "最大回撤", "values": ["-11%", "-6%"], "best": "max"},
        {"label": "回撤天数", "values": [40, 20], "best": "min"},
        {"label": "夏普", "values": [0.9, 0.9], "best": "max"},
        {"label": "规模", "values": [120.0, 73.0]},
    ],
}


def test_duel_card_scores_each_metric():
    b = _block(DUEL)
    assert render.entity_wins(b) == [[0, 0], [1, 1], [2, 1], [3, 0], [3, 1]]
    assert render.tally(b) == {"wins": [["甲", 1], ["乙", 2]], "ties": 1}
    out = render.block_html(b)
    assert 'class="ent is-duel"' in out
    assert '<b class="">甲 胜 1 项</b><span>持平 1</span><b class="is-win">乙 胜 2 项</b>' in out
    assert '<div class="duel-val v-1">73</div>' in out


def test_entities_one_and_many():
    one = render.block_html(_block({"type": "entities", "items": [{"name": "甲", "note": "老将"}], "metrics": [{"label": "年化", "values": ["11%"]}]}))
    assert 'class="ent is-one"' in one and "ent-fact-v" in one
    grid = _block({"type": "entities", "items": [{"name": n} for n in "ABC"], "metrics": [{"label": "收益", "values": [1, 3, 2], "best": "max"}]})
    out = render.block_html(grid)
    assert 'class="ent is-grid"' in out and '<span class="best">3</span>' in out
    assert "胜出项：A 0 · B 1 · C 0" in out


def test_scorecard_table_shows_tally():
    b = _block({"type": "table", "columns": ["指标", "甲", "乙"], "rows": [["收益", 1, 2], ["回撤", -3, -2], ["夏普", 1, 1]], "row_best": ["max", "max", "max"]})
    assert render.tally(b) == {"wins": [["甲", 0], ["乙", 2]], "ties": 1}
    assert "胜出项：甲 0 · 乙 2（持平 1）" in render.block_html(b, "表 1")


def test_fit_block():
    out = render.block_html(_block({"type": "fit", "good": ["能扛 10% 回撤"], "bad": ["一年内要用钱"]}))
    assert 'fit-good"><div class="fit-label">适合</div><ul><li>能扛 10% 回撤</li>' in out
    with pytest.raises(ValueError):
        _block({"type": "fit"})


def test_percentile_and_contrib_charts():
    pct = ChartBlock.model_validate(
        {"type": "chart", "chart": "percentile", "x": ["收益", "回撤"], "series": [{"name": "前", "data": [12, 70]}],
         "display": ["+8.2%", "-11%"], "x_label": "同类 1,203 只"}
    )
    svg = chart_svg(pct)
    assert "前 12%" in svg and "后 30%" in svg and "同类中位" in svg and svg.count('class="c-hit"') == 2
    with pytest.raises(ValueError):
        ChartBlock.model_validate({"type": "chart", "chart": "percentile", "x": ["a"], "series": [{"name": "x", "data": [120]}]})
    contrib = ChartBlock.model_validate(
        {"type": "chart", "chart": "contrib", "x": ["股", "债"], "series": [{"name": "仓位", "data": [40, 60]}, {"name": "贡献", "data": [85, 15]}]}
    )
    svg = chart_svg(contrib)
    assert render.CONTRIB_BASE in svg and svg.count('class="c-hit"') == 2
    with pytest.raises(ValueError):
        ChartBlock.model_validate({"type": "chart", "chart": "contrib", "x": ["a"], "series": [{"name": "x", "data": [1]}]})


def test_long_label_bars_turn_horizontal():
    short = ChartBlock.model_validate({"type": "chart", "chart": "bar", "x": ["甲", "乙"], "series": [{"name": "a", "data": [1, 0]}]})
    long = ChartBlock.model_validate(
        {"type": "chart", "chart": "bar", "x": ["一个很长的名字A", "一个很长的名字B", "C", "D"], "series": [{"name": "a", "data": [1, 2, 3, 4]}]}
    )
    assert not render.bar_is_horizontal(short) and render.bar_is_horizontal(long)
    assert 'class="c-guide"' in chart_svg(short) and 'class="c-guide"' not in chart_svg(long)
