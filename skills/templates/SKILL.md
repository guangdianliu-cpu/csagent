---
name: templates
description: 利得官方 Word 研报、PPT 演示文稿与 PDF 导出引擎。依据官方母版与视觉规范（深蓝 #40527E、标准排版、官方红头与合规免责声明）生成高保真 .docx、.pptx 和 .pdf 文件。当用户要求导出 Word 报告、研报文档、PPT 汇报、图表或演示文稿时使用。
version: 2.2.0
tags: [word, ppt, docx, pptx, pdf, chart, report, presentation, template, export]
allowed-tools: [write_script, run_script, read_file, write_file, html_to_pdf, view_image, send_file]
enabled: true
---

# 利得官方高保真文档与 PPT 模板导出规范

> **核心原则：以官方构建器为准，允许受约束的扩展**
>
> 1. **默认基于官方构建器**：优先直接调用 `skills/templates/scripts` 下的官方构建器 `LeadDocBuilder` 与 `LeadPPTBuilder`。母版背景、公司 LOGO、字号层级、利得深蓝（#40527E）配色及文末法律声明已由构建器固化。
> 2. **允许会话级继承扩展**：当现有构建器缺少业务所需的特殊版式时，**允许在会话 scripts 目录（`scripts/tools/{session_id}/` 或会话沙箱）编写扩展脚本**，但必须严格遵守三条硬约束：
>    - **约束一（基类继承）**：必须以 `LeadDocBuilder` 或 `LeadPPTBuilder` 为基类派生扩展（继承免责声明锚点、红头、母版背景、色板），**严禁脱离基类从零创建无母版的空白 `Document()` 或 `Presentation()`**。
>    - **约束二（规范约束）**：新增版式只准引用 `COLOR_*`、`PALETTE` 常量与已固化的字号/字体层级（正文 10.5pt、中文统一微软雅黑，西文 Arial/微软雅黑成对设置），杜绝视觉风格漂移。
>    - **约束三（扩展留痕）**：扩展脚本留在会话脚本目录即可，并在脚本开头注释写明新增了什么版式、为什么需要。**不要在给用户的回复里讲扩展实现细节**——用户只需要拿到文件。

---

## 0. 环境前置自检 (Preflight Check)

在执行报告生成任务或首次转档 PDF 前，建议调用 `check_env.py` 探明当前运行环境的基础设施状态（转换引擎、中文字体、预览工具）：

```python
# run_script: script="<skill:templates>/scripts/check_env.py"
# 探测 LibreOffice / Word COM / PowerPoint COM、中文字体库与目检渲染器
```

若环境无可用 PDF 转换引擎，只交付 `.docx` 或 `.pptx` 并在回复中向用户说明，不要改走 HTML 路线冒充官方模板。

---

## 1. 投研图表快速绘制 (Matplotlib)

若报告或 PPT 中需要插入净值走势图、收益率柱状图或行业分布饼图，使用 `lead_chart_style` 快速生成利得深蓝主题 300 DPI 高清 PNG：

```python
import os
import sys
from pathlib import Path
# 按项目根定位构建器，不依赖当前工作目录（CSAGENT_PROJECT_ROOT 由 run_script 注入）
sys.path.insert(0, os.path.join(os.environ["CSAGENT_PROJECT_ROOT"], "skills", "templates", "scripts"))
from lead_chart_style import plot_line_chart, plot_bar_chart, plot_pie_chart

# 净值走势折线图
plot_line_chart(
    x=["2026-01", "2026-03", "2026-05", "2026-07", "2026-09"],
    series_dict={"本基金": [1.0, 1.08, 1.15, 1.12, 1.24], "业绩基准": [1.0, 1.02, 1.05, 1.03, 1.09]},
    title="近1年累计净值走势对比",
    output_path=os.path.join(os.environ["CSAGENT_OUTBOUND_DIR"], "nav_curve.png")
)
```

---

## 2. 生成高保真官方 PPT 演示文稿 (`.pptx` / `.pdf`)

支持 8 类核心版式：封面、目录、卡片总览、高清图表、左右分栏、双对象对比、数据表格、正文要点与官方封底。
已集成文本溢出防护（Auto-shrink 自适应降字号）与一键转官方 PDF。

### 快速调用范式

```python
import os
import sys
from pathlib import Path

# 按项目根定位构建器，不依赖当前工作目录（CSAGENT_PROJECT_ROOT 由 run_script 注入）
sys.path.insert(0, os.path.join(os.environ["CSAGENT_PROJECT_ROOT"], "skills", "templates", "scripts"))
from ppt_builder import LeadPPTBuilder

builder = LeadPPTBuilder()

# 1. 封面页
builder.set_cover("纳斯达克100指数基金研究分析", "利得基金内部投研报告", "2026年9月")

# 2. 目录页 (P2)
builder.add_agenda_slide(["指数基本面与特征", "重点产品多维对比", "收益与估值走势", "资产配置策略"])

# 3. 核心指标卡片页（Slide 10 风格，带字符自适应防溢出）
builder.add_cards_slide(
    "核心统计总览",
    [
        {"title": "覆盖基金数", "value": "12 只", "desc": "包含主流公募及QDII联接"},
        {"title": "总管理规模", "value": "458 亿元", "desc": "近1年规模持续增长"},
        {"title": "年化平均收益", "value": "+24.5%", "desc": "跑赢同类QDII平均基准"},
        {"title": "平均夏普比率", "value": "1.82", "desc": "风险收益比表现突出"},
    ],
    subtitle="数据来源：利得投研数据库"
)

# 4. 高清图表页 (P1/P2)
builder.add_chart_slide(
    "近1年净值走势与超额收益",
    os.path.join(os.environ["CSAGENT_OUTBOUND_DIR"], "nav_curve.png"),
    notes=["AI算力龙头持续驱动盈利高增", "夏普比率显著优于传统成熟市场大盘", "估值回调提供了极佳的逢低配置窗口"],
    subtitle="数据来源：利得投研数据库"
)

# 5. 左右分栏混排页 (P2，Slide 12 风格)
builder.add_two_column_slide(
    "宏观驱动与投资建议",
    left_bullets=["美联储进入降息周期，海外流动性改善", "科技巨头自由现金流充沛，研发壁垒高"],
    right_bullets=["建议采取分批定投平滑短期波动", "核心仓位首选流动性好、规模适中的ETF"],
    left_title="宏观与产业驱动",
    right_title="投资与风控建议"
)

# 6. 双对象对比页 (P2)
builder.add_comparison_slide(
    "代表性产品深度对比",
    item_a={"name": "国泰纳斯达克100 (513100)", "metrics": [("最新规模", "120.5亿"), ("管理费率", "0.50%")], "pros": ["场内换手活跃，流动性好"]},
    item_b={"name": "广发纳指100 (159941)", "metrics": [("最新规模", "186.2亿"), ("管理费率", "0.50%")], "pros": ["规模全市场第一，跟踪误差极小"]}
)

# 7. 标准数据表格页 (Slide 9 风格)
builder.add_table_slide(
    "全市场挂钩产品汇总",
    ["基金代码", "基金简称", "管理人", "近1年收益", "规模(亿)"],
    [
        ["513100", "国泰纳斯达克100ETF", "国泰基金", "+23.9%", "120.5"],
        ["159941", "广发纳指100ETF", "广发基金", "+25.8%", "186.2"],
    ]
)

# 8. 保存导出（自动将官方封底置于末尾）
out_dir = os.environ["CSAGENT_OUTBOUND_DIR"]  # 本会话的产出目录，由 run_script 注入
out_path = Path(out_dir) / "nasdaq100_presentation.pptx"
builder.save(out_path)
print(f"PPT 成功生成: {out_path}")

# 9. 一键转官方 PDF (P0，如需 PDF 交付或目检)
try:
    pdf_path = builder.to_pdf(Path(out_dir) / "nasdaq100_presentation.pdf")
    print(f"PPT PDF 成功生成: {pdf_path}")
except Exception as e:
    print(f"PPT PDF 转换提示: {e}")
```

---

## 3. 生成高保真 Word 研报与 PDF (`.docx` / `.pdf`)

遵循中文正式研报排版标准：
- **正文排版**：`add_paragraph` 默认首行缩进 2 字符（21pt）、两端对齐（Justified），彻底杜绝程序拼接感。
- **标题控制**：`add_heading_1/2/3` 默认设置 `keep_with_next=True`，杜绝标题在页末孤立（Orphan heading）。
- **指标卡片**：`add_kpi_cards` 支持 2~4 个大字号量化核心指标卡片并排展示（大字号数值 + 标签 + 补充说明）。
- **引用块增强**：`add_callout` 默认采用利得深蓝左侧 4.5pt 粗边框与 `#F2F5FA` 浅底色，形成研报标准 Takeaway 样式。
- **项目符号规范**：`add_bullet` 统一采用实心圆点 `•`（\u2022）并配利得深蓝主题色。
- **表格高保真**：`add_table` 默认配置浅灰细网格边框（#DCE1EB）、东亚字符权重动态列宽自适应（彻底解决中文表头换行挤出）、单元格全量垂直居中、跨页自动重复表头（`w:tblHeader`）以及单行防断开截断（`w:cantSplit`）。
- **结构完整**：红头抬头发函、核心观点引用块、插入高清图表 (P1)、分页符 (P3)、合并单元格表格 (P3) 与一键转官方 PDF (P0)。

### PDF 两条路线的取舍（重要）

- **走官方模板的正式研报与演示文稿** → 用本 skill 的 `doc.to_pdf()` 或 `builder.to_pdf()`（基于 LibreOffice 或 Office COM）。
  若环境无转换引擎会抛出 `PdfEngineNotFoundError`，此时只交付 `.docx` / `.pptx` 并在回复中说明，不要改走 HTML 路线冒充官方 PDF。
- **自由排版的长图文 / 非模板内容** → 用系统工具 `html_to_pdf`（先写完整 HTML 到 outbound，再转换）。
- **PPT 转 PDF 与渲染目检**：本 skill 已原生支持 `builder.to_pdf()`，转出 PDF 后可调用 `pdf_preview.py` 目检排版。

### 快速调用范式

```python
import os
import sys
from pathlib import Path

# 按项目根定位构建器，不依赖当前工作目录（CSAGENT_PROJECT_ROOT 由 run_script 注入）
sys.path.insert(0, os.path.join(os.environ["CSAGENT_PROJECT_ROOT"], "skills", "templates", "scripts"))
from doc_builder import LeadDocBuilder

doc = LeadDocBuilder()

# 1. 官方抬头四格表
doc.set_meta(report_type="ETF专题研究报告", department="利得研究", company="上海利得基金销售有限公司基金报告")

# 2. 报告标题与核心指标卡片格栅
doc.add_title("纳斯达克100指数基金配置价值深度分析", subtitle="——全球科技龙头的核心配置工具")
doc.add_kpi_cards([
    {"value": "120.5 亿", "label": "挂钩产品总规模", "sub": "场内流动性充沛"},
    {"value": "+23.9%", "label": "近1年收益率", "sub": "跑赢主流宽基"},
    {"value": "0.50%", "label": "主流管理费率", "sub": "费率低廉优势"},
])

# 3. 核心观点卡片（带深蓝左侧粗边线）
doc.add_callout("当前估值处于历史中位，伴随算力基建加速落地，业绩成长韧性显著，建议作为中长期核心卫星资产配置。", title="核心投资观点")

# 4. 章节正文与图表嵌入 (P0 首行缩进 2 字符 + 两端对齐，P1 图表)
doc.add_heading_1("一、指数收益特征与净值走势")
doc.add_paragraph("纳斯达克100指数汇聚了纳斯达克市场中市值最大、流动性最好的100只非金融类优质股票。")
doc.add_bullet("成分股集中度高：前十大重仓股涵盖全球头部科技龙头企业。")
doc.add_chart_image(os.path.join(os.environ["CSAGENT_OUTBOUND_DIR"], "nav_curve.png"), caption="图1：近1年净值累计走势对比（数据来源：利得投研数据库）", width_inches=5.5)

# 5. 插入分页符 (P3)
doc.add_page_break()

# 6. 表格（P0 浅灰边框 + 中西文列宽动态加权 + 单元格垂直居中 + P3 跨页重复表头与 cantSplit）
doc.add_heading_1("二、挂钩产品费率与流动性明细")
doc.add_table(
    headers=["基金代码", "基金简称", "管理费率", "托管费率", "规模(亿元)"],
    rows=[
        ["513100", "国泰纳斯达克100ETF", "0.50%", "0.15%", "120.5"],
        ["159941", "广发纳指100ETF", "0.50%", "0.15%", "186.2"],
    ]
)

# 6. 保存 Word（末尾自动保留官方法律声明）
out_dir = os.environ["CSAGENT_OUTBOUND_DIR"]  # 本会话的产出目录，由 run_script 注入
docx_path = Path(out_dir) / "nasdaq100_report.docx"
doc.save(docx_path)
print(f"Word 成功生成: {docx_path}")

# 7. 一键转官方 PDF (P0，如需 PDF 交付)
try:
    pdf_path = doc.to_pdf(Path(out_dir) / "nasdaq100_report.pdf")
    print(f"PDF 成功生成: {pdf_path}")
except Exception as e:
    print(f"PDF 转换提示: {e}")  # 无引擎时只交付 docx，并向用户说明
```

---

## 4. 会话级版式扩展规范

当遇到构建器未内置的特殊版式（如“三基金并行对比卡片”、“多层穿透持仓树状表”等）时，按方案 2 在会话脚本中**继承基类扩展**：

```python
import os
import sys
from pathlib import Path
# 按项目根定位构建器，不依赖当前工作目录（CSAGENT_PROJECT_ROOT 由 run_script 注入）
sys.path.insert(0, os.path.join(os.environ["CSAGENT_PROJECT_ROOT"], "skills", "templates", "scripts"))

from ppt_builder import LeadPPTBuilder, COLOR_PRIMARY, PALETTE, COLOR_CARD_BG, COLOR_BORDER, _set_ppt_font
from pptx.util import Inches, Pt
from pptx.enum.shapes import MSO_SHAPE

class CustomPPTBuilder(LeadPPTBuilder):
    """会话级扩展：新增三对象横向对比版式。"""

    def add_three_column_comparison(self, title: str, items: list[dict]):
        slide = self.prs.slides.add_slide(self._content_layout)
        self.add_title(slide, title)

        col_w = Inches(2.65)
        col_h = Inches(3.6)
        top = Inches(1.3)
        gap = Inches(0.2)

        for idx, item in enumerate(items[:3]):
            x = Inches(0.8) + idx * (col_w + gap)
            theme_color = PALETTE[idx % len(PALETTE)]

            # 遵循官方卡片底色与边框
            card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, top, col_w, col_h)
            card.fill.solid()
            card.fill.fore_color.rgb = COLOR_CARD_BG
            card.line.color.rgb = COLOR_BORDER

            # 顶部色标
            bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, top, col_w, Inches(0.4))
            bar.fill.solid()
            bar.fill.fore_color.rgb = theme_color
            p_bar = bar.text_frame.paragraphs[0]
            p_bar.text = item.get("name", f"产品 {idx+1}")
            p_bar.font.size = Pt(11)
            p_bar.font.bold = True
            _set_ppt_font(p_bar)

        return slide
```

**留痕**：在扩展脚本开头用注释写明新增版式的名称与用途（例如「三产品并列对比版式：继承 `LeadPPTBuilder`，新增 `add_three_column_comparison`」）。回复用户时只交付文件，不讲扩展实现。

---

## 5. 排版安全与长度预算红线 (Budget Guidance)

代码具备弹性自适应，但模型应在上游控制文字与内容密度，从源头杜绝风格漂移与排版溢出：

| 载体 | 元素 | 建议容量上限 | 超限处理策略 |
|---|---|---|---|
| **PPT** | 单页核心要点 | 建议 ≤ 4 条，单条 25~35 字 | 超过 4 条或总字数 > 150 字，主动拆分为左右分栏页或分为两页呈现 |
| **PPT** | 多卡片总览页 | 建议 2~4 个指标卡片 | 超过 4 个卡片进入紧凑模式，数值控制在 10 字符内，描述控制在 25 字内 |
| **PPT** | 图表页右侧要点 | 建议 3 条观察结论 | 超出 3 条字号将自动收缩至 9.5pt，字数过多建议精炼提炼结论核心 |
| **Word** | 正文段落 | 连续正文建议 ≤ 3 段 | 超过 3 段应插入二级标题或 Callout 引用框切分视觉层次 |
| **Word** | 数据表格 | 建议行数 ≤ 12 行，列数 ≤ 6 列 | 超过 12 行自动跨页重复表头；超过 6 列宽表应精简字段或拆为多维指标表 |

---

## 6. 生成后自检与转档工具链（两级质量把控体系）

盲生成无法发现表格截断、图片变形、样式异常或分页跨度问题。流水线推荐采用**「两级质检体系」**：
1. **第一级（结构核查·必选）**：秒级执行、零成本，使用 `check_doc.py` / `check_ppt.py` 拦截页数缺失、空白页、数据表格或图表未嵌入等结构硬伤；
2. **第二级（视觉抽查·推荐）**：转出 PDF 后调用 `pdf_preview.py` 抽取关键页（如封面、核心图表页），使用系统内置的 `view_image` 工具让多模态大模型审阅真实渲染效果，拦截文字溢出截断、排版重叠、字体变形等视觉隐患。

### 6.1 独立格式转档（DOCX / PPTX -> 高保真 PDF）
若在会话中直接构建了 `.docx` 或 `.pptx`，可随时调用官方 CLI 工具转档：
```python
# Word 转 PDF:
# run_script: script="<skill:templates>/scripts/docx_to_pdf.py", args=["data/uploads/.../outbound/report.docx"]

# PPT 转 PDF:
# run_script: script="<skill:templates>/scripts/pptx_to_pdf.py", args=["data/uploads/.../outbound/deck.pptx"]
```

### 6.2 第一级：文档结构与内容安全自查（代码级快速自检，免手写 COM 脚本）
若需验证生成产物的页数、段落字数、标题层级、嵌入图表与表格维度，推荐优先调用纯 Python 结构自检工具（强容错、跨平台、秒级执行）：

#### 6.2.1 Word 文档自检 (check_doc.py)
```python
# run_script: script="<skill:templates>/scripts/check_doc.py", args=["data/uploads/.../outbound/report.docx"]
# 或以 JSON 结构输出：
# args: ["data/uploads/.../outbound/report.docx", "--json"]
```
*特点：纯 Python 解析，对合并单元格与空段落强容错，彻底杜绝手写 Word COM 导致的 `NoneType.NameLocal` 或 `(-2147352567, '发生意外。')` 异常。*

#### 6.2.2 PPT 演示文稿自检 (check_ppt.py)
```python
# run_script: script="<skill:templates>/scripts/check_ppt.py", args=["data/uploads/.../outbound/deck.pptx"]
# 或以 JSON 结构输出：
# args: ["data/uploads/.../outbound/deck.pptx", "--json"]
```
*特点：纯 Python 解析总页数、大纲标题、16:9/4:3 比例、嵌入图表数与数据表格行列，自动侦测空白页。*

### 6.3 第二级：视觉多页预览与多模态质检（逐页 PNG 提取 + view_image 审阅）
转出 `.pdf` 后，可调用 `pdf_preview.py` 提取前 N 页高清 PNG，再使用 `view_image` 工具抽查关键页面（如封面、核心图表页、多栏对比页）：
```python
# 步骤 1：抽取关键页预览图（例如前 3-5 页）
# run_script: script="<skill:templates>/scripts/pdf_preview.py", args=["data/uploads/.../outbound/deck.pdf", "data/uploads/.../outbound/preview", "5"]

# 步骤 2：调用系统内置 view_image 工具对关键页执行多模态排版审阅
# view_image(path="outbound/preview/deck-p1.png", prompt="检查封面排版是否协调、主副标题是否完整无截断、LOGO是否正常呈现")
```
*特点：多模态视觉模型对渲染截图进行直观审阅，精准捕捉纯代码无法感知的文字溢出、重叠遮挡、图表拉伸错位与字体异常。*

---

## 7. 生成后发送给用户

生成完 `.pptx`、`.docx` 或 `.pdf` 后，调用 `send_file` 将产物发送给用户：

```python
send_file(file_path="data/uploads/.../outbound/nasdaq100_report.docx")
```
