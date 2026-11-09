"""利得基金官方 Matplotlib 投研图表主题与绘图工具 (LeadChartStyle)。

严格遵循官方 VI 视觉规范：
  - 主色曲线：#40527E（利得深蓝）
  - 对比色系：#AF5B5B（砖红）, #E19056（暖橙）, #38768F（青蓝）, #73608E（紫灰）, #80BFBE（薄荷绿）
  - 极简图表风格：隐藏上/右边框，极简灰色虚线辅助网格，微软雅黑字体
  - 导出高分辨率 300 DPI PNG 图表，无缝嵌入 Word 报告与 PPT 幻灯片。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import matplotlib
import matplotlib.pyplot as plt

# 官方配色常数
COLOR_PRIMARY = "#40527E"  # 主深蓝
COLOR_ACCENT1 = "#AF5B5B"  # 砖红（首选对比线，如基准指数）
COLOR_ACCENT2 = "#E19056"  # 暖橙
COLOR_ACCENT3 = "#38768F"  # 青蓝
COLOR_ACCENT4 = "#73608E"  # 紫灰
COLOR_ACCENT5 = "#80BFBE"  # 薄荷绿
COLOR_TEXT = "#505359"     # 正文文字色
COLOR_MUTED = "#858C98"    # 刻度与注释色
COLOR_GRID = "#E2E7EE"     # 网格虚线

PALETTE = [
    COLOR_PRIMARY,
    COLOR_ACCENT1,
    COLOR_ACCENT2,
    COLOR_ACCENT3,
    COLOR_ACCENT4,
    COLOR_ACCENT5,
]


def apply_lead_theme() -> None:
    """应用利得基金官方图表主题设置到全局 rcParams。"""
    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "WenQuanYi Micro Hei",
        "sans-serif",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
    matplotlib.rcParams["figure.facecolor"] = "#FFFFFF"
    matplotlib.rcParams["axes.facecolor"] = "#FFFFFF"
    matplotlib.rcParams["axes.edgecolor"] = "#D0D6E2"
    matplotlib.rcParams["axes.linewidth"] = 0.8
    matplotlib.rcParams["text.color"] = COLOR_TEXT
    matplotlib.rcParams["axes.labelcolor"] = COLOR_TEXT
    matplotlib.rcParams["xtick.color"] = COLOR_MUTED
    matplotlib.rcParams["ytick.color"] = COLOR_MUTED
    matplotlib.rcParams["grid.color"] = COLOR_GRID
    matplotlib.rcParams["grid.linestyle"] = "--"
    matplotlib.rcParams["grid.linewidth"] = 0.6
    matplotlib.rcParams["legend.frameon"] = True
    matplotlib.rcParams["legend.framealpha"] = 0.95
    matplotlib.rcParams["legend.edgecolor"] = "#E2E7EE"


def polish_ax(ax: plt.Axes, *, title: str = "", xlabel: str = "", ylabel: str = "") -> None:
    """对单个坐标系应用极简金融图表风格（隐藏顶/右脊柱，浅灰横向网格）。"""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#D0D6E2")
    ax.spines["bottom"].set_color("#D0D6E2")
    ax.yaxis.grid(True, linestyle="--", alpha=0.7, color=COLOR_GRID)
    ax.xaxis.grid(False)

    if title:
        ax.set_title(title, fontsize=12, fontweight="bold", color=COLOR_PRIMARY, pad=12)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=10, color=COLOR_TEXT)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10, color=COLOR_TEXT)


def plot_line_chart(
    x: Sequence[Any],
    series_dict: dict[str, Sequence[float]],
    *,
    title: str = "净值走势对比",
    ylabel: str = "累计净值 / 收益率(%)",
    output_path: str | Path,
    figsize: tuple[float, float] = (8.0, 4.2),
) -> Path:
    """快速绘制一条或多条走势折线图，保存为 300 DPI PNG。

    :param x: 横轴数据列表（如日期字符串）
    :param series_dict: 曲线字典，如 {"本基金": [1.0, 1.1, ...], "沪深300": [1.0, 0.98, ...]}
    :param output_path: 输出 PNG 路径
    """
    apply_lead_theme()
    fig, ax = plt.subplots(figsize=figsize, dpi=300)

    for idx, (label, y_vals) in enumerate(series_dict.items()):
        color = PALETTE[idx % len(PALETTE)]
        lw = 2.2 if idx == 0 else 1.6
        linestyle = "-" if idx == 0 else "--"
        ax.plot(x, y_vals, label=label, color=color, linewidth=lw, linestyle=linestyle)

    polish_ax(ax, title=title, ylabel=ylabel)
    ax.legend(loc="best", fontsize=9)

    # 横轴刻度抽样显示（防止日期过密拥挤）
    if len(x) > 10:
        step = max(1, len(x) // 6)
        ax.set_xticks(range(0, len(x), step))
        ax.set_xticklabels([str(x[i]) for i in range(0, len(x), step)], rotation=15, ha="right")

    fig.tight_layout()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_bar_chart(
    categories: Sequence[str] | None = None,
    values: Sequence[float] | None = None,
    *,
    x: Sequence[str] | None = None,
    y: Sequence[float] | None = None,
    title: str = "收益与指标对比",
    ylabel: str = "",
    output_path: str | Path,
    figsize: tuple[float, float] = (7.5, 3.8),
    colors: Sequence[str] | None = None,
) -> Path:
    """快速绘制利得深蓝主题柱状图。

    支持 categories/values 与 x/y 形参别名，彻底避免因命名习惯差异导致的 TypeError。
    """
    cats = categories if categories is not None else x
    vals = values if values is not None else y
    if cats is None or vals is None:
        raise ValueError("plot_bar_chart 必须提供 categories (或 x) 与 values (或 y) 数据")

    apply_lead_theme()
    fig, ax = plt.subplots(figsize=figsize, dpi=300)

    bar_colors = colors or [PALETTE[i % len(PALETTE)] for i in range(len(cats))]
    bars = ax.bar(cats, vals, color=bar_colors, width=0.55, edgecolor="none")

    # 柱顶数值标注
    for bar in bars:
        h = bar.get_height()
        va = "bottom" if h >= 0 else "top"
        ax.annotate(
            f"{h:.1f}%" if "%" in ylabel or "收益" in title else f"{h:.1f}",
            xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 3 if h >= 0 else -10),
            textcoords="offset points",
            ha="center",
            va=va,
            fontsize=9,
            color=COLOR_TEXT,
            fontweight="bold",
        )

    polish_ax(ax, title=title, ylabel=ylabel)
    if len(cats) > 5:
        plt.xticks(rotation=20, ha="right")

    fig.tight_layout()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_pie_chart(
    labels: Sequence[str] | None = None,
    sizes: Sequence[float] | None = None,
    *,
    categories: Sequence[str] | None = None,
    values: Sequence[float] | None = None,
    x: Sequence[str] | None = None,
    y: Sequence[float] | None = None,
    title: str = "行业与资产分布",
    output_path: str | Path,
    figsize: tuple[float, float] = (6.0, 4.0),
) -> Path:
    """绘制高颜值环形/饼图。

    支持 labels/sizes、categories/values 与 x/y 形参别名。
    """
    lbls = labels if labels is not None else (categories or x)
    szs = sizes if sizes is not None else (values or y)
    if lbls is None or szs is None:
        raise ValueError("plot_pie_chart 必须提供 labels (或 categories/x) 与 sizes (或 values/y) 数据")

    apply_lead_theme()
    fig, ax = plt.subplots(figsize=figsize, dpi=300)

    colors = [PALETTE[i % len(PALETTE)] for i in range(len(lbls))]
    wedges, texts, autotexts = ax.pie(
        szs,
        labels=lbls,
        autopct="%1.1f%%",
        startangle=90,
        colors=colors,
        pctdistance=0.75,
        textprops=dict(color=COLOR_TEXT, fontsize=9),
        wedgeprops=dict(width=0.45, edgecolor="#FFFFFF", linewidth=1.5),  # 甜甜圈环形
    )
    for at in autotexts:
        at.set_fontsize(8.5)
        at.set_color("#FFFFFF")
        at.set_weight("bold")

    ax.set_title(title, fontsize=12, fontweight="bold", color=COLOR_PRIMARY, pad=12)
    fig.tight_layout()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out
