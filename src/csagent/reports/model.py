"""报告的结构：模型交出的是数据（积木），不是 HTML；前端与导出统一渲染。

docs/对话页升级方案.md §3.2。骨架固定（报告头 → 核心观点 → 目录 → 编号章节 → 风险提示 →
数据来源），模型只填内容；章节号、图表号、目录由渲染层生成。校验不过就把可读的错误
还给模型重试，不落盘。

M7/M8 存下的旧报告只有平铺的 ``blocks``，读出来当成「一个无标题章节」显示（``body_sections``）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Cell = str | int | float | None


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkdownBlock(_Block):
    type: Literal["markdown"]
    text: str = Field(..., min_length=1, max_length=20000)


class TableBlock(_Block):
    type: Literal["table"]
    title: str = Field(default="", max_length=60)
    # 表下方「资料来源：…」；空则不显示
    source: str = Field(default="", max_length=120)
    columns: list[str] = Field(..., min_length=1, max_length=30)
    rows: list[list[Cell]] = Field(..., max_length=500)
    # 这些列里的数按正负着色（涨红跌绿）
    signed_columns: list[str] = Field(default_factory=list)
    # 记分卡：自动标出最优格。row_best 每行一个方向（行是指标、列是对象，第 1 列是指标名不参与）；
    # col_best 按列给方向（行是对象、列是指标）。max = 越大越好，min = 越小越好，null = 这一行 / 列不比
    row_best: list[Literal["max", "min"] | None] = Field(default_factory=list, max_length=500)
    col_best: dict[str, Literal["max", "min"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> TableBlock:
        width = len(self.columns)
        for i, row in enumerate(self.rows, 1):
            if len(row) != width:
                raise ValueError(f"第 {i} 行有 {len(row)} 格，表头是 {width} 列")
        unknown = [c for c in [*self.signed_columns, *self.col_best] if c not in self.columns]
        if unknown:
            raise ValueError(f"signed_columns / col_best 里的列不在表头里：{'、'.join(unknown)}")
        if self.row_best and len(self.row_best) != len(self.rows):
            raise ValueError(f"row_best 有 {len(self.row_best)} 项，表格有 {len(self.rows)} 行")
        return self


class ChartSeries(_Block):
    name: str = Field(..., min_length=1, max_length=40)
    data: list[float | None] = Field(..., min_length=1, max_length=500)


class ScatterPoint(_Block):
    name: str = Field(..., min_length=1, max_length=20)
    x: float
    y: float


class ChartBlock(_Block):
    """line / bar / pie / rank / percentile / contrib 用 x + series；scatter 用 points + x_label / y_label。

    - rank：横向排名条，单系列，渲染时按数值从大到小排。
    - percentile：分位条，单系列，data 是「同类前 x%」（越小越好）；display 是每项显示的原始值，
      x_label 写同类口径（如「同类 1,203 只」）。
    - contrib：贡献条，两个系列（第 1 个是占比 / 权重，第 2 个是贡献），看「占多少、贡献多少」。
    """

    type: Literal["chart"]
    chart: Literal["line", "bar", "pie", "rank", "scatter", "percentile", "contrib"]
    title: str = Field(default="", max_length=60)
    # 数值单位（如 %、亿元），显示在纵轴上方和悬停提示里
    unit: str = Field(default="", max_length=8)
    source: str = Field(default="", max_length=120)
    x: list[str] = Field(default_factory=list, max_length=500)
    series: list[ChartSeries] = Field(default_factory=list, max_length=10)
    points: list[ScatterPoint] = Field(default_factory=list, max_length=60)
    x_label: str = Field(default="", max_length=20)
    y_label: str = Field(default="", max_length=20)
    display: list[Annotated[str, Field(max_length=16)]] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def _shape(self) -> ChartBlock:
        if self.chart == "scatter":
            if len(self.points) < 2:
                raise ValueError("散点图 points 至少 2 个点")
            if not self.x_label or not self.y_label:
                raise ValueError("散点图要写 x_label 和 y_label（横轴、纵轴是什么指标）")
            return self
        if not self.x or not self.series:
            raise ValueError(f"{self.chart} 图要有 x 和 series")
        for s in self.series:
            if len(s.data) != len(self.x):
                raise ValueError(f"系列「{s.name}」有 {len(s.data)} 个点，x 有 {len(self.x)} 个")
        if self.chart in ("pie", "rank") and len(self.series) != 1:
            raise ValueError("饼图 / 排名条只能有 1 个系列")
        if self.chart == "rank" and len(self.x) > 30:
            raise ValueError("排名条最多 30 项，多了只放前 30")
        if self.chart == "percentile":
            if len(self.series) != 1 or len(self.x) > 12:
                raise ValueError("分位条只能有 1 个系列，最多 12 项")
            if any(v is None or not 0 <= v <= 100 for v in self.series[0].data):
                raise ValueError("分位条的 data 是「同类前 x%」，每项都要有，取 0–100")
        if self.chart == "contrib" and (len(self.series) != 2 or len(self.x) > 12):
            raise ValueError("贡献条要 2 个系列（占比、贡献），最多 12 项")
        if self.display and len(self.display) != len(self.x):
            raise ValueError(f"display 有 {len(self.display)} 项，x 有 {len(self.x)} 项")
        return self


class ConclusionBlock(_Block):
    type: Literal["conclusion"]
    text: str = Field(..., min_length=1, max_length=2000)


class Entity(_Block):
    name: str = Field(..., min_length=1, max_length=20)
    # 类型 / 身份，一个短词
    kind: str = Field(default="", max_length=16)
    tags: list[Annotated[str, Field(min_length=1, max_length=10)]] = Field(default_factory=list, max_length=4)
    # 一句话画像
    note: str = Field(default="", max_length=60)


class EntityMetric(_Block):
    label: str = Field(..., min_length=1, max_length=12)
    values: list[Cell] = Field(..., min_length=1, max_length=5)
    # 这项谁更好：max 越大越好，min 越小越好，null 不比
    best: Literal["max", "min"] | None = None


class EntitiesBlock(_Block):
    """对象卡：1 个 = 画像，2 个 = 对决（逐项比、各赢几项），3–5 个 = 并排卡。"""

    type: Literal["entities"]
    title: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=120)
    items: list[Entity] = Field(..., min_length=1, max_length=5)
    metrics: list[EntityMetric] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _shape(self) -> EntitiesBlock:
        for m in self.metrics:
            if len(m.values) != len(self.items):
                raise ValueError(f"指标「{m.label}」有 {len(m.values)} 个值，对象有 {len(self.items)} 个")
        return self


class FitBlock(_Block):
    """适合 / 不适合 两栏：给建议、做适当性提示用。"""

    type: Literal["fit"]
    title: str = Field(default="", max_length=40)
    good: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list, max_length=5)
    bad: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list, max_length=5)
    good_label: str = Field(default="适合", max_length=8)
    bad_label: str = Field(default="不适合", max_length=8)

    @model_validator(mode="after")
    def _shape(self) -> FitBlock:
        if not self.good and not self.bad:
            raise ValueError("fit 至少要有 good 或 bad 一栏")
        return self


Block = Annotated[
    MarkdownBlock | TableBlock | ChartBlock | ConclusionBlock | EntitiesBlock | FitBlock,
    Field(discriminator="type"),
]


class Section(_Block):
    """一个章节：标题由模型写，「一、二、三」由渲染层编。"""

    title: str = Field(..., min_length=1, max_length=40)
    # 本节结论：一句话，加粗显示在章节标题下
    lead: str = Field(default="", max_length=120)
    blocks: list[Block] = Field(..., min_length=1, max_length=30)


# 核心观点的语气标记：优势 / 权衡 / 风险 / 建议；info = 不打标记
Tone = Literal["plus", "tradeoff", "risk", "action", "info"]


class KeyPoint(_Block):
    text: str = Field(..., min_length=1, max_length=120)
    tone: Tone = "info"


Point = Annotated[str, Field(min_length=1, max_length=120)] | KeyPoint


class Kpi(_Block):
    """报告头的指标卡：一个大数字 + 标签 + 一行注释。"""

    label: str = Field(..., min_length=1, max_length=16)
    value: str = Field(..., min_length=1, max_length=16)
    note: str = Field(default="", max_length=30)
    # auto = 按数值正负涨红跌绿；neutral = 不着色；up / down 强制
    tone: Literal["auto", "neutral", "up", "down"] = "auto"


class ReportContent(BaseModel):
    """模型交来的报告本体。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=60)
    summary: str = Field(..., min_length=1, max_length=120)
    # 数据截至日，如 2026-10-28；显示在报告头
    as_of: str = Field(default="", max_length=20)
    # 一句话结论，报告头最醒目的位置
    verdict: str = Field(default="", max_length=60)
    kpis: list[Kpi] = Field(default_factory=list, max_length=4)
    # 核心观点：固定放在最前面；可以是纯文字，也可以带语气标记
    key_points: list[Point] = Field(default_factory=list, max_length=6)
    sections: list[Section] = Field(default_factory=list, max_length=12)
    # 旧格式（M7/M8）：平铺积木，新报告不再用
    blocks: list[Block] = Field(default_factory=list, max_length=50)
    # 本报告特有的风险提示；通用文案由配置补在后面
    risk_note: str = Field(default="", max_length=600)
    sources: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _has_body(self) -> ReportContent:
        if not self.sections and not self.blocks:
            raise ValueError("sections 不能为空：至少一个章节")
        total = sum(len(s.blocks) for s in self.sections) + len(self.blocks)
        if total > 60:
            raise ValueError(f"内容块共 {total} 个，上限 60")
        return self

    def body_sections(self) -> list[Section]:
        """渲染用：旧报告的平铺积木包成一个无标题章节。"""
        if self.sections:
            return list(self.sections)
        return [Section.model_construct(title="", blocks=list(self.blocks))]

    def all_blocks(self) -> list:
        return [b for s in self.body_sections() for b in s.blocks]

    def points(self) -> list[KeyPoint]:
        """核心观点统一成 KeyPoint（纯文字 = 不打标记）。"""
        return [p if isinstance(p, KeyPoint) else KeyPoint(text=p) for p in self.key_points]


class Report(ReportContent):
    """落盘的一版报告。"""

    report_id: str
    version: int
    created_at: float


def readable_errors(exc: ValidationError, limit: int = 5) -> str:
    """把校验错误压成一行，给模型看得懂、改得动。"""
    parts = []
    for err in exc.errors()[:limit]:
        where = ".".join(str(x) for x in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "；".join(parts)
