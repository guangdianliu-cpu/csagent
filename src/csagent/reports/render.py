"""报告积木 → HTML / SVG，供 PDF 导出与面板里的图表共用。

- 图表一律在服务端画成 SVG：面板直接嵌、PDF 原样打印，两边一模一样；WeasyPrint 不跑 JS，
  前端图表库进不了 PDF。
- PDF 里的 markdown 用下面这个小转换器：只覆盖模型实际会写的那几样（标题、段落、列表、
  引用、表格、代码、粗斜体、链接），不为它引依赖。面板里仍用 marked + DOMPurify。

版式对齐 docs/对话页升级方案.md §2.6 / §2.7：主色 #6C6CC1，涨 #E85951、跌 #48C993。
"""

from __future__ import annotations

import html
import math
import re
import time

from csagent.reports.model import ChartBlock, Report

ACCENT = "#6C6CC1"
UP = "#E85951"
DOWN = "#48C993"
MUTED = "#838894"
GRID = "#ECEDF1"
# 图表：扁平、低饱和，相邻色明度接近、色相拉开，不靠高对比区分（与 report.js 的 PALETTE 一致）
PALETTE = (
    "#8088DB", "#F2BC77", "#83BDE8", "#8FD0BF", "#E8A6B8",
    "#B7A6E2", "#AEB4BF", "#D7C095", "#A3C98F", "#E6A98A",
)
AXIS = "#A4A8B2"
CHART_GRID = "#F1F2F5"

# 与前端 format.js 的 SIGNED_PCT_RE 同一口径：前面不是字母数字的 +x% / -x%
_SIGNED_PCT_RE = re.compile(r"(^|[^0-9A-Za-z.%])([+＋\-−–])(\s?\d+(?:\.\d+)?\s?%)")


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


# 「18894.0 亿」→「1.89 万亿」：五位数以上的亿读不出来（与 report.js 的 tidyText 一致）
_YI_RE = re.compile(r"(^|[^\d.,])(\d{1,3}(?:,\d{3})+|\d{5,})(\.\d+)?\s*亿")


def tidy_text(text: str) -> str:
    def sub(m: re.Match) -> str:
        v = float(m.group(2).replace(",", "") + (m.group(3) or ""))
        if v < 10000:
            return m.group(0)
        return f"{m.group(1)}{_fmt(round(v / 10000, 2))} 万亿"

    return _YI_RE.sub(sub, text)


def display_cell(value: object) -> str:
    """单元格显示：73.0 → 73，18894 → 18,894；文字原样（只换算「万亿」）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return f"{value:,}" if abs(value) >= 10000 else repr(value)
        return f"{int(value):,}" if abs(value) >= 10000 else str(int(value))
    return tidy_text(str(value))


def signed_class(value: object) -> str:
    """表格单元格的正负：返回 ``num-up`` / ``num-down`` / 空串。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return "num-up" if value > 0 else "num-down" if value < 0 else ""
    s = str(value).strip().replace("−", "-").replace("–", "-").replace("＋", "+")
    m = re.match(r"^([+-]?)\s*(\d+(?:\.\d+)?)", s)
    if not m or float(m.group(2)) == 0:
        return ""
    return "num-down" if m.group(1) == "-" else "num-up"


# ------------------------------------------------------------------ markdown


def _color_pct(escaped: str) -> str:
    def sub(m: re.Match) -> str:
        cls = "num-down" if m.group(2) in "-−–" else "num-up"
        return f'{m.group(1)}<span class="{cls}">{m.group(2)}{m.group(3)}</span>'

    return _SIGNED_PCT_RE.sub(sub, escaped)


def _inline(text: str) -> str:
    """行内：先转义，再认 `code`、**粗**、*斜*、[链接](http…)，最后给正负百分数上色。"""
    parts = re.split(r"(`[^`]+`)", tidy_text(text))
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{esc(part[1:-1])}</code>")
            continue
        s = esc(part)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", s)
        s = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            r'<a href="\2">\1</a>',
            s,
        )
        out.append(_color_pct(s))
    return "".join(out)


_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    """items: (缩进, 是否有序, 文本)。按缩进嵌套。"""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (缩进, 标签)
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            out.append("</li>")
        out.append(f"<li>{_inline(text)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


# 「**论点。** 展开……」这种粗体开头的段落：论点单独一行，展开的话放下面，读的时候先扫论点
_CLAIM_RE = re.compile(r"^\*\*(.+?)\*\*\s*[：:，,]?\s*(.*)$")
CLAIM_MIN_BODY = 8


def _claim(lines: list[str]) -> str:
    m = _CLAIM_RE.match(lines[0])
    if not m:
        return ""
    head = m.group(1).rstrip("：:")
    body = [x for x in [m.group(2), *lines[1:]] if x]
    if sum(len(x) for x in body) < CLAIM_MIN_BODY:
        return ""
    return (
        f'<div class="claim"><div class="claim-head">{_inline(head)}</div>'
        f'<p class="claim-body">{"<br>".join(_inline(x) for x in body)}</p></div>'
    )


def markdown_to_html(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            claim = _claim(para)
            out.append(claim or "<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{esc(chr(10).join(code))}</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = min(len(m.group(1)), 4)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^([-*_])\1{2,}$", stripped.replace(" ", "")):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + markdown_to_html("\n".join(quote)) + "</blockquote>")
            continue
        if "|" in stripped and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            head = _split_row(stripped)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_table_html(head, rows))
            continue
        if _LIST_RE.match(line):
            flush_para()
            items = []
            while i < len(lines):
                lm = _LIST_RE.match(lines[i])
                if not lm:
                    # 列表项的续行（缩进的普通文字）并进上一项
                    if lines[i].startswith((" ", "\t")) and lines[i].strip() and items:
                        ind, ordered, txt = items[-1]
                        items[-1] = (ind, ordered, txt + " " + lines[i].strip())
                        i += 1
                        continue
                    break
                indent = len(lm.group(1).replace("\t", "    "))
                items.append((indent, lm.group(2)[0].isdigit(), lm.group(3)))
                i += 1
            out.append(_render_list(items))
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return "".join(out)


# ------------------------------------------------------------------ 表格


def cell_number(value: object) -> float | None:
    """表格单元格里的数：-3.2 / "+8.2%" / "1,234" → 浮点；取不出返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"^\s*([+\-−–＋]?)\s*(\d[\d,]*(?:\.\d+)?)", str(value))
    if not m:
        return None
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) in "-−–" and m.group(1) else v


def _pick(cells: list[tuple[int, int, float]], direction: str) -> list[list[int]]:
    """一组 (行, 列, 数) 里的最优格；不到两个数不比。并列都算。"""
    if len(cells) < 2:
        return []
    target = max(v for *_, v in cells) if direction == "max" else min(v for *_, v in cells)
    return [[r, c] for r, c, v in cells if v == target]


def _row_groups(block) -> list[list[list[int]]]:
    """记分卡每一项比较的胜出格，一项一组。"""
    groups = []
    for r, direction in enumerate(block.row_best):
        if direction:
            row = block.rows[r]
            nums = [(r, c, n) for c in range(1, len(row)) if (n := cell_number(row[c])) is not None]
            groups.append(_pick(nums, direction))
    for col, direction in block.col_best.items():
        c = block.columns.index(col)
        nums = [(r, c, n) for r, row in enumerate(block.rows) if (n := cell_number(row[c])) is not None]
        groups.append(_pick(nums, direction))
    return [g for g in groups if g]


def best_cells(block) -> list[list[int]]:
    """记分卡的最优格 [[行, 列], …]：row_best 逐行比（跳过第 1 列指标名），col_best 逐列比。并列都标。"""
    if block.type == "entities":
        return entity_wins(block)
    return [cell for g in _row_groups(block) for cell in g]


def entity_wins(block) -> list[list[int]]:
    """对象卡每项指标的胜出 [[指标下标, 对象下标], …]。"""
    out = []
    for mi, m in enumerate(block.metrics):
        if m.best:
            nums = [(mi, k, n) for k, v in enumerate(m.values) if (n := cell_number(v)) is not None]
            out.extend(_pick(nums, m.best))
    return out


def tally(block) -> dict | None:
    """各赢几项：{"wins": [[名字, 独胜项数], …], "ties": 并列项数}；没有可比的项返回 None。

    表格：row_best 时对象是列（第 2 列起），col_best 时对象是行（第 1 列是名字）。
    """
    if block.type == "entities":
        names = [it.name for it in block.items]
        groups: dict[int, list[int]] = {}
        for mi, k in entity_wins(block):
            groups.setdefault(mi, []).append(k)
        winners = list(groups.values())
    elif block.row_best:
        names = [str(c) for c in block.columns[1:]]
        winners = [[c - 1 for _, c in g] for g in _row_groups(block)]
    elif block.col_best:
        names = [display_cell(row[0]) for row in block.rows]
        winners = [[r for r, _ in g] for g in _row_groups(block)]
    else:
        return None
    if not winners or len(names) < 2:
        return None
    wins = [0] * len(names)
    ties = 0
    for w in winners:
        if len(w) == 1:
            wins[w[0]] += 1
        else:
            ties += 1
    return {"wins": [[n, c] for n, c in zip(names, wins)], "ties": ties}


def tally_text(t: dict | None) -> str:
    if not t:
        return ""
    text = " · ".join(f"{n} {c}" for n, c in t["wins"])
    return f"胜出项：{text}" + (f"（持平 {t['ties']}）" if t["ties"] else "")


def _table_html(
    columns: list[str],
    rows: list[list],
    signed: set[int] | None = None,
    title: str = "",
    best: list[list[int]] | None = None,
) -> str:
    signed = signed or set()
    marked = {(r, c) for r, c in (best or [])}
    head = "".join(f"<th>{_inline(str(c))}</th>" for c in columns)
    body = []
    for i, row in enumerate(rows):
        cells = []
        for j, v in enumerate(row):
            cls = signed_class(v) if j in signed else ""
            text = display_cell(v)
            inner = esc(text) if cls else _inline(text)
            if (i, j) in marked:
                inner = f'<span class="best">{inner}</span>'
            cells.append(f'<td class="{cls}">{inner}</td>' if cls else f"<td>{inner}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    cap = f'<div class="tbl-title">{esc(title)}</div>' if title else ""
    return (
        f'{cap}<div class="tbl-wrap"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


# ------------------------------------------------------------------ 图表


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    if lo == hi:
        lo, hi = (lo - 1, hi + 1) if lo == 0 else (lo - abs(lo) * 0.1, hi + abs(hi) * 0.1)
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    v = start
    while v < hi + step * 0.999:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def chart_svg(block: ChartBlock) -> str:
    if block.chart == "pie":
        return _pie_svg(block)
    if block.chart == "rank":
        return _rank_svg(block)
    if block.chart == "scatter":
        return _scatter_svg(block)
    if block.chart == "percentile":
        return _percentile_svg(block)
    if block.chart == "contrib" or (block.chart == "bar" and bar_is_horizontal(block)):
        return _hbar_svg(block)
    return _xy_svg(block)


def bar_is_horizontal(block: ChartBlock) -> bool:
    """柱子多、或标签长到底下写不下时，柱状图改横向（与 report.js 无关，服务端说了算）。"""
    n = len(block.x)
    slot = (400 - 48) / max(n, 1)
    longest = max((len(x) for x in block.x), default=0)
    return n > 6 or longest * 11.5 > slot or n * len(block.series) > 16


def _svg_open(w: float, h: float) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h:.0f}" width="100%" '
        f'font-family="PingFang SC, Microsoft YaHei, sans-serif" role="img">'
    )


# 排名条正负两色：与 PALETTE 同样低饱和，仍守「涨红跌绿」
RANK_UP = "#EE9E97"
RANK_DOWN = "#8FCFB1"


def _rank_svg(block: ChartBlock) -> str:
    """横向排名条：按数值从大到小，正负分色，数值标在条尾。感应区每行一块（data-i 是原下标）。"""
    W, row_h, label_w, value_w = 400, 26, 76, 58
    data = [(i, name, v) for i, (name, v) in enumerate(zip(block.x, block.series[0].data)) if v is not None]
    data.sort(key=lambda t: t[2], reverse=True)
    top = 16 if block.unit else 4
    H = top + row_h * len(data) + 4
    lo, hi = min(0.0, *(v for *_, v in data)), max(0.0, *(v for *_, v in data))
    span = (hi - lo) or 1.0
    x0, x1 = label_w, W - value_w
    zero = x0 + (0 - lo) / span * (x1 - x0)
    signed = lo < 0
    out = [_svg_open(W, H)]
    if block.unit:
        out.append(f'<text x="0" y="10" font-size="11" fill="{AXIS}">单位：{esc(block.unit)}</text>')
    if signed:
        out.append(f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="{top}" y2="{H - 4}" stroke="{CHART_GRID}"/>')
    for k, (i, name, v) in enumerate(data):
        y = top + row_h * k
        cy = y + row_h / 2
        end = x0 + (v - lo) / span * (x1 - x0)
        bx, width = min(zero, end), abs(end - zero)
        color = (RANK_DOWN if v < 0 else RANK_UP) if signed else PALETTE[0]
        label = name if len(name) <= 6 else name[:6] + "…"
        out.append(
            f'<text x="{label_w - 8}" y="{cy + 4:.1f}" font-size="12" fill="#4E5361" text-anchor="end">{esc(label)}</text>'
            f'<rect class="c-bar" data-i="{i}" x="{bx:.1f}" y="{cy - 7:.1f}" width="{max(width, 1):.1f}" '
            f'height="14" rx="3" fill="{color}"/>'
            f'<text x="{x1 + 6}" y="{cy + 4:.1f}" font-size="12" fill="{MUTED}">{"+" if signed and v > 0 else ""}{esc(_fmt(v))}{esc(block.unit if block.unit == "%" else "")}</text>'
        )
    out.append('<g class="c-hits">')
    for k, (i, _, _) in enumerate(data):
        out.append(
            f'<rect class="c-hit" data-i="{i}" x="0" y="{top + row_h * k}" width="{W}" height="{row_h}" fill="transparent"/>'
        )
    out.append("</g></svg>")
    return "".join(out)


# 贡献条里「占比」那一条用灰，突出「贡献」
CONTRIB_BASE = "#D5D8E0"


def _hbar_svg(block: ChartBlock) -> str:
    """横向分组柱：每个 x 一行，每个系列一条，数值写在条尾。contrib 是它的特例（占比灰、贡献按正负）。"""
    W, value_w = 400, 58
    # 标签栏按最长的名字放宽（最多 9 个字），横排就是为了标签写得下
    longest = max((len(x) for x in block.x), default=2)
    label_w = min(9, max(3, longest)) * 12 + 10
    k = len(block.series)
    bar_h, gap = 10, 6 if k > 1 else 0
    row_h = k * bar_h + (k - 1) * gap + 14
    contrib = block.chart == "contrib"
    values = [v for s in block.series for v in s.data if v is not None]
    lo, hi = min(0.0, *values) if values else 0.0, max(0.0, *values) if values else 1.0
    span = (hi - lo) or 1.0
    x0, x1 = label_w, W - value_w
    zero = x0 + (0 - lo) / span * (x1 - x0)
    signed = lo < 0

    def color(si: int, v: float) -> str:
        if contrib:
            if si == 0:
                return CONTRIB_BASE
            return (RANK_DOWN if v < 0 else RANK_UP) if signed else PALETTE[0]
        return PALETTE[si % len(PALETTE)]

    out: list[str] = []
    top = 4
    if block.unit:
        out.append(f'<text x="0" y="10" font-size="11" fill="{AXIS}">单位：{esc(block.unit)}</text>')
        top = 16
    if k > 1:
        names = [s.name for s in block.series]
        colors = [CONTRIB_BASE, RANK_UP if signed else PALETTE[0]] if contrib else None
        legend, ly = _legend(names, 120 if block.unit else label_w, 11, W - 120, colors)
        out.append(legend)
        top = max(top, ly + 8)
    H = top + row_h * len(block.x) + 4
    if signed:
        out.append(f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="{top}" y2="{H - 4}" stroke="{CHART_GRID}"/>')
    for i, name in enumerate(block.x):
        y = top + row_h * i + 7
        label = name if len(name) <= 9 else name[:8] + "…"
        out.append(
            f'<text x="{label_w - 8}" y="{y + (row_h - 14) / 2 + 4:.1f}" font-size="12" fill="#4E5361" '
            f'text-anchor="end">{esc(label)}</text>'
        )
        for si, s in enumerate(block.series):
            v = s.data[i]
            if v is None:
                continue
            by = y + si * (bar_h + gap)
            end = x0 + (v - lo) / span * (x1 - x0)
            bx, width = min(zero, end), abs(end - zero)
            unit = block.unit if block.unit == "%" else ""
            out.append(
                f'<rect class="c-bar" data-i="{i}" x="{bx:.1f}" y="{by:.1f}" width="{max(width, 1.5):.1f}" '
                f'height="{bar_h}" rx="2" fill="{color(si, v)}"/>'
                f'<text x="{x1 + 6}" y="{by + bar_h - 1:.1f}" font-size="11" fill="{MUTED}">'
                f'{"+" if signed and v > 0 else ""}{esc(_fmt(v))}{esc(unit)}</text>'
            )
    out.append('<g class="c-hits">')
    for i in range(len(block.x)):
        out.append(
            f'<rect class="c-hit" data-i="{i}" x="0" y="{top + row_h * i}" width="{W}" height="{row_h}" fill="transparent"/>'
        )
    out.append("</g></svg>")
    return _svg_open(W, H) + "".join(out)


PCT_GOOD = "#6C6CC1"
PCT_MID = "#8A8FA0"
PCT_BAD = "#C2A06A"


def percentile_label(top: float) -> str:
    """同类前 x% → 「前 12%」/「后 30%」。"""
    return f"前 {_fmt(top)}%" if top <= 50 else f"后 {_fmt(round(100 - top, 2))}%"


def _percentile_svg(block: ChartBlock) -> str:
    """分位条：一行一项，轨道左差右好，圆点是它在同类里的位置，竖虚线是同类中位。"""
    W, row_h, pad = 400, 44, 8
    top = 22
    data = block.series[0].data
    H = top + row_h * len(block.x) + 16
    tx0, tx1 = pad, W - pad
    mid = (tx0 + tx1) / 2
    out = [_svg_open(W, H)]
    if block.x_label:
        out.append(f'<text x="0" y="12" font-size="11" fill="{AXIS}">{esc(block.x_label)}</text>')
    out.append(f'<text x="{W}" y="12" font-size="11" fill="{AXIS}" text-anchor="end">越靠右越好 →</text>')
    for i, (name, t) in enumerate(zip(block.x, data)):
        y = top + row_h * i
        pos = tx0 + (100 - t) / 100 * (tx1 - tx0)
        color = PCT_GOOD if t <= 25 else PCT_BAD if t > 75 else PCT_MID
        shown = block.display[i] if block.display else ""
        out.append(
            f'<text x="0" y="{y + 14}" font-size="12" fill="#4E5361">{esc(name[:12])}'
            + (f'<tspan fill="#1F2329" font-weight="600" dx="6">{esc(shown)}</tspan>' if shown else "")
            + "</text>"
            f'<text x="{W}" y="{y + 14}" font-size="12" font-weight="600" fill="{color}" text-anchor="end">'
            f"{percentile_label(t)}</text>"
            f'<rect x="{tx0}" y="{y + 22}" width="{tx1 - tx0}" height="6" rx="3" fill="{CHART_GRID}"/>'
            f'<rect x="{tx0}" y="{y + 22}" width="{max(pos - tx0, 0):.1f}" height="6" rx="3" fill="{color}" fill-opacity="0.28"/>'
            f'<circle class="c-bar" data-i="{i}" cx="{pos:.1f}" cy="{y + 25}" r="6" fill="{color}" stroke="#fff" stroke-width="2"/>'
        )
    out.append(
        f'<line x1="{mid}" x2="{mid}" y1="{top + 16}" y2="{H - 14}" stroke="#C9CCD6" stroke-dasharray="3 3"/>'
        f'<text x="{mid}" y="{H - 2}" font-size="10" fill="{AXIS}" text-anchor="middle">同类中位</text>'
    )
    out.append('<g class="c-hits">')
    for i in range(len(block.x)):
        out.append(
            f'<rect class="c-hit" data-i="{i}" x="0" y="{top + row_h * i}" width="{W}" height="{row_h}" fill="transparent"/>'
        )
    out.append("</g></svg>")
    return "".join(out)


def _scatter_svg(block: ChartBlock) -> str:
    """散点：横轴 x_label、纵轴 y_label，点旁写名字。感应区就是点本身（放大的透明圆）。"""
    W, H = 400, 270
    left, right, top, bottom = 44, 14, 22, 40
    pw, ph = W - left - right, H - top - bottom
    xs = _nice_ticks(min(p.x for p in block.points), max(p.x for p in block.points))
    ys = _nice_ticks(min(p.y for p in block.points), max(p.y for p in block.points))

    def px(v: float) -> float:
        return left + (v - xs[0]) / (xs[-1] - xs[0]) * pw

    def py(v: float) -> float:
        return top + ph - (v - ys[0]) / (ys[-1] - ys[0]) * ph

    out = [_svg_open(W, H)]
    out.append(f'<text x="0" y="12" font-size="11" fill="{AXIS}">{esc(block.y_label)}{esc(block.unit and "（" + block.unit + "）")}</text>')
    for t in ys:
        y = py(t)
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{CHART_GRID}"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" font-size="11" fill="{AXIS}" text-anchor="end">{_fmt(t)}</text>')
    for t in xs:
        x = px(t)
        out.append(f'<text x="{x:.1f}" y="{top + ph + 16}" font-size="11" fill="{AXIS}" text-anchor="middle">{_fmt(t)}</text>')
    out.append(f'<text x="{W - right}" y="{H - 6}" font-size="11" fill="{AXIS}" text-anchor="end">{esc(block.x_label)} →</text>')
    for i, pt in enumerate(block.points):
        x, y = px(pt.x), py(pt.y)
        color = PALETTE[i % len(PALETTE)]
        anchor, dx = ("end", -10) if x > left + pw * 0.75 else ("start", 10)
        out.append(
            f'<circle class="c-bar" data-i="{i}" cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}" stroke="#fff" stroke-width="2"/>'
            f'<text x="{x + dx:.1f}" y="{y + 4:.1f}" font-size="12" fill="#4E5361" text-anchor="{anchor}">{esc(pt.name)}</text>'
        )
    out.append('<g class="c-hits">')
    for i, pt in enumerate(block.points):
        out.append(f'<circle class="c-hit" data-i="{i}" cx="{px(pt.x):.1f}" cy="{py(pt.y):.1f}" r="16" fill="transparent"/>')
    out.append("</g></svg>")
    return "".join(out)


def _legend(
    names: list[str], x0: float, y: float, width: float, colors: list[str] | None = None
) -> tuple[str, float]:
    """图例横排，放不下换行；返回 (svg, 最后一行的 y)。"""
    parts = []
    x, row_h = x0, 18
    for k, name in enumerate(names):
        w = 15 + 12 * len(name) + 12
        if x + w > x0 + width and x > x0:
            x = x0
            y += row_h
        color = colors[k] if colors else PALETTE[k % len(PALETTE)]
        parts.append(
            f'<circle cx="{x + 4:.1f}" cy="{y - 4:.1f}" r="4" fill="{color}"/>'
            f'<text x="{x + 13:.1f}" y="{y:.1f}" font-size="12" fill="{MUTED}">{esc(name)}</text>'
        )
        x += w
    return "".join(parts), y


def _xy_svg(block: ChartBlock) -> str:
    # 画布按手机宽度（~343px）设计：缩放接近 1:1，字不会糊；PDF 里由 CSS 限宽
    W, H = 400, 260
    # 图题在 HTML 里（「图 n 标题」），画布里只留单位
    left, right, top = 40, 8, 26 if block.unit else 12
    names = [s.name for s in block.series]
    # 图例从底部第一行往下排；排了几行，画布就加高几行
    legend, legend_y = _legend(names, left, H - 10, W - left - right)
    extra = legend_y - (H - 10)
    H2, bottom = H + extra, 44 + extra
    plot_w, plot_h = W - left - right, H2 - top - bottom

    values = [v for s in block.series for v in s.data if v is not None]
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    if block.chart == "bar":
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    ticks = _nice_ticks(lo, hi)
    y_lo, y_hi = ticks[0], ticks[-1]

    def yp(v: float) -> float:
        return top + plot_h - (v - y_lo) / (y_hi - y_lo) * plot_h

    n = len(block.x)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H2}" width="100%" '
        f'font-family="PingFang SC, Microsoft YaHei, sans-serif" role="img">'
    ]
    if block.unit:
        out.append(f'<text x="0" y="12" font-size="11" fill="{AXIS}">单位：{esc(block.unit)}</text>')
    for t in ticks:
        y = yp(t)
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{CHART_GRID}"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" font-size="11" fill="{AXIS}" text-anchor="end">{_fmt(t)}</text>')

    slot = plot_w / n
    label_every = max(1, math.ceil(n / 6))
    for i, label in enumerate(block.x):
        if i % label_every and i != n - 1:
            continue
        cx = left + slot * (i + 0.5)
        out.append(
            f'<text x="{cx:.1f}" y="{top + plot_h + 18:.1f}" font-size="11" fill="{AXIS}" '
            f'text-anchor="middle">{esc(label)}</text>'
        )

    if block.chart == "bar":
        k = len(block.series)
        group = slot * (0.56 if k == 1 else 0.72)
        bw = group / k
        zero = yp(0.0)
        for si, s in enumerate(block.series):
            color = PALETTE[si % len(PALETTE)]
            for i, v in enumerate(s.data):
                if v is None:
                    continue
                x = left + slot * i + (slot - group) / 2 + bw * si
                y = yp(v)
                out.append(
                    f'<rect class="c-bar" data-i="{i}" x="{x:.1f}" y="{min(y, zero):.1f}" '
                    f'width="{max(bw - 3, 1):.1f}" height="{max(abs(zero - y), 1.5):.1f}" rx="3" fill="{color}"/>'
                )
    else:
        base = yp(0.0) if y_lo <= 0 <= y_hi else top + plot_h
        for si, s in enumerate(block.series):
            color = PALETTE[si % len(PALETTE)]
            seg: list[str] = []
            paths = []
            for i, v in enumerate(s.data):
                if v is None:
                    if seg:
                        paths.append(seg)
                    seg = []
                    continue
                seg.append(f"{left + slot * (i + 0.5):.1f},{yp(v):.1f}")
            if seg:
                paths.append(seg)
            for p in paths:
                if len(block.series) == 1 and len(p) > 1:
                    x0, x1 = p[0].split(",")[0], p[-1].split(",")[0]
                    out.append(
                        f'<polygon points="{x0},{base:.1f} {" ".join(p)} {x1},{base:.1f}" '
                        f'fill="{color}" fill-opacity="0.12"/>'
                    )
                out.append(
                    f'<polyline points="{" ".join(p)}" fill="none" stroke="{color}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>'
                )
                if len(p) == 1:  # 孤点连不成线，留个点
                    x, y = p[0].split(",")
                    out.append(f'<circle cx="{x}" cy="{y}" r="2.5" fill="{color}"/>')
        for i in range(n):
            dots = []
            for si, s in enumerate(block.series):
                v = s.data[i] if i < len(s.data) else None
                if v is None:
                    continue
                color = PALETTE[si % len(PALETTE)]
                dots.append(
                    f'<circle cx="{left + slot * (i + 0.5):.1f}" cy="{yp(v):.1f}" r="4" fill="{color}" '
                    f'stroke="#fff" stroke-width="2"/>'
                )
            out.append(f'<g class="c-dots" data-i="{i}" visibility="hidden">{"".join(dots)}</g>')
    out.append(legend)
    # 交互感应区：报告页悬停 / 点按时显示参考线和数值（report.js）；透明，PDF 里看不见
    out.append(
        f'<g class="c-hits"><line class="c-guide" x1="0" x2="0" y1="{top}" y2="{top + plot_h:.1f}" '
        f'stroke="#C9CCD6" stroke-dasharray="3 3" visibility="hidden"/>'
    )
    for i in range(n):
        out.append(
            f'<rect class="c-hit" data-i="{i}" data-cx="{left + slot * (i + 0.5):.1f}" x="{left + slot * i:.1f}" '
            f'y="{top}" width="{slot:.1f}" height="{plot_h:.1f}" fill="transparent"/>'
        )
    out.append("</g></svg>")
    return "".join(out)


def _pie_svg(block: ChartBlock) -> str:
    W, H = 400, 220
    top = 12
    cx, cy, r = 92, top + (H - top) / 2, min(84, (H - top) / 2 - 8)
    # (原下标, 名称, 值)：下标给报告页的悬停提示对回原数据
    data = [(i, label, v or 0.0) for i, (label, v) in enumerate(zip(block.x, block.series[0].data))]
    data = [(i, label, v) for i, label, v in data if v > 0]
    total = sum(v for _, _, v in data) or 1.0
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" '
        f'font-family="PingFang SC, Microsoft YaHei, sans-serif" role="img">'
    ]
    # 环形图：扁平，扇区之间留白缝
    ri = r * 0.58
    angle = -math.pi / 2
    for k, (idx, _, v) in enumerate(data):
        color = PALETTE[k % len(PALETTE)]
        frac = v / total
        if frac >= 0.9999:
            out.append(
                f'<circle class="c-slice" data-i="{idx}" cx="{cx}" cy="{cy:.1f}" r="{(r + ri) / 2:.1f}" '
                f'fill="none" stroke="{color}" stroke-width="{r - ri:.1f}"/>'
            )
            break
        end = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0

        def pt(radius: float, a: float) -> str:
            return f"{cx + radius * math.cos(a):.1f},{cy + radius * math.sin(a):.1f}"

        out.append(
            f'<path class="c-slice" data-i="{idx}" '
            f'd="M{pt(r, angle)} A{r:.1f},{r:.1f} 0 {large} 1 {pt(r, end)} '
            f'L{pt(ri, end)} A{ri:.1f},{ri:.1f} 0 {large} 0 {pt(ri, angle)} Z" '
            f'fill="{color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>'
        )
        angle = end
    # 右侧图例：名称 + 占比
    ly = top + 16
    for k, (_, label, v) in enumerate(data[:9]):
        color = PALETTE[k % len(PALETTE)]
        out.append(
            f'<circle cx="200" cy="{ly - 4:.1f}" r="4" fill="{color}"/>'
            f'<text x="212" y="{ly:.1f}" font-size="12" fill="#1F2329">{esc(label[:12])}</text>'
            f'<text x="398" y="{ly:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{v / total:.1%}</text>'
        )
        ly += 20
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ 骨架与编号

_CN_NUM = "一二三四五六七八九十"
# 章节少于这个数不出目录：三章一屏就看完了，目录只是占地方（与 report.js 一致）
TOC_MIN = 4


def cn_number(n: int) -> str:
    """1 → 一，11 → 十一（章节最多 12 个）。"""
    if n <= 10:
        return _CN_NUM[n - 1]
    return "十" + _CN_NUM[n - 11]


def numbered(report: Report) -> list[tuple[str, list[tuple[object, str]]]]:
    """[(章节标题带编号, [(积木, 图表编号)])]。图、表各自全篇连续编号，与 report.js 同一口径。"""
    out = []
    fig = tab = 0
    sections = report.body_sections()
    for si, sec in enumerate(sections, 1):
        heading = f"{cn_number(si)}、{sec.title}" if sec.title else ""
        items = []
        for b in sec.blocks:
            label = ""
            if b.type == "table":
                tab += 1
                label = f"表 {tab}"
            elif b.type == "chart":
                fig += 1
                label = f"图 {fig}"
            items.append((b, label))
        out.append((heading, items))
    return out


# ------------------------------------------------------------------ 整份 HTML（PDF 用）


def _css_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _pdf_css(title: str, footer: str) -> str:
    return f"""
@page {{
  size: A4; margin: 20mm 16mm 18mm;
  @top-left {{ content: {_css_string(title[:40])}; font-size: 9px; color: {MUTED}; }}
  @bottom-left {{ content: {_css_string(footer)}; font-size: 9px; color: {MUTED}; }}
  @bottom-right {{ content: "第 " counter(page) " / " counter(pages) " 页"; font-size: 9px; color: {MUTED}; }}
}}
* {{ box-sizing: border-box; }}
body {{ font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  color: #1F2329; font-size: 13px; line-height: 1.75; margin: 0; }}
.cover {{ padding-bottom: 12px; margin-bottom: 16px; border-bottom: 2px solid {ACCENT}; }}
.cover-kind {{ color: {ACCENT}; font-size: 12px; font-weight: 600; letter-spacing: 0.1em; }}
.cover h1 {{ font-size: 22px; line-height: 1.4; margin: 6px 0; }}
.cover-summary {{ color: #4E5361; font-size: 14px; margin: 0; }}
.cover-meta {{ color: {MUTED}; font-size: 11px; margin-top: 8px; }}
.cover-meta span + span::before {{ content: " · "; }}
.points {{ background: rgba(108,108,193,0.07); border-radius: 6px; padding: 10px 14px 10px; margin: 0 0 16px;
  page-break-inside: avoid; }}
.box-title {{ color: {ACCENT}; font-weight: 600; font-size: 13px; margin-bottom: 4px; }}
.points ol {{ margin: 0; padding-left: 20px; }}
.points li {{ margin: 3px 0; }}
.tone {{ display: inline-block; font-size: 10px; line-height: 16px; padding: 0 5px; border-radius: 3px;
  margin-right: 6px; vertical-align: 1px; }}
.tone-plus {{ background: #E3F4EC; color: #1F7A55; }}
.tone-tradeoff {{ background: #FBF0DC; color: #8A5A12; }}
.tone-risk {{ background: #FBE9E8; color: #A33A33; }}
.tone-action {{ background: #ECECFA; color: #4B4BA6; }}
.verdict {{ background: #EEEEFA; color: #34347E; border-radius: 6px; padding: 10px 14px; margin: 0 0 12px;
  font-size: 15px; font-weight: 600; page-break-inside: avoid; }}
.verdict-label {{ color: {ACCENT}; font-size: 11px; font-weight: 600; margin-bottom: 2px; }}
.kpis {{ display: flex; gap: 8px; margin: 0 0 14px; page-break-inside: avoid; }}
.kpi {{ flex: 1; background: #F7F8FA; border-radius: 6px; padding: 8px 10px; }}
.kpi-label {{ color: {MUTED}; font-size: 11px; }}
.kpi-value {{ font-size: 20px; font-weight: 600; line-height: 1.3; }}
.kpi-note {{ color: {MUTED}; font-size: 10px; }}
.sec-lead {{ font-weight: 600; margin: 0 0 8px; break-after: avoid; page-break-after: avoid; }}
.best {{ background: #ECECFA; padding: 0 4px; border-radius: 3px; font-weight: 600; }}
.toc {{ margin: 0 0 16px; font-size: 12px; color: #4E5361; }}
.toc ol {{ list-style: none; padding: 0; margin: 0; columns: 2; }}
.sec {{ margin-top: 18px; }}
.sec-title {{ font-size: 17px; line-height: 1.4; margin: 0 0 8px; padding-left: 10px; border-left: 4px solid {ACCENT};
  page-break-after: avoid; }}
.md h1, .md h2 {{ font-size: 15px; margin: 12px 0 6px; }}
.md h3, .md h4 {{ font-size: 14px; margin: 10px 0 4px; }}
p {{ margin: 6px 0; }}
ul, ol {{ padding-left: 20px; margin: 6px 0; }}
blockquote {{ margin: 8px 0; padding: 2px 10px; border-left: 3px solid {GRID}; color: {MUTED}; font-size: 12px; }}
code {{ background: #F3F3F5; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
pre {{ background: #F7F7F9; padding: 8px 10px; border-radius: 6px; white-space: pre-wrap; }}
pre code {{ background: none; padding: 0; }}
a {{ color: {ACCENT}; }}
.fig {{ margin: 12px 0 14px; }}
.fig-cap {{ font-weight: 600; font-size: 13px; margin-bottom: 6px; page-break-after: avoid; }}
.fig-cap b {{ color: {ACCENT}; margin-right: 6px; }}
.fig-src {{ color: {MUTED}; font-size: 11px; margin-top: 4px; break-before: avoid; page-break-before: avoid; }}
.fig.is-chart {{ page-break-inside: avoid; }}
.fig svg {{ display: block; max-width: 520px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; page-break-inside: auto; }}
tr {{ page-break-inside: avoid; }}
thead {{ display: table-header-group; }}
th {{ background: #F3F3F3; font-weight: 600; text-align: left; }}
th, td {{ border: 1px solid {GRID}; padding: 5px 8px; vertical-align: top; }}
.num-up {{ color: {UP}; white-space: nowrap; }}
.num-down {{ color: {DOWN}; white-space: nowrap; }}
.conclusion {{ background: rgba(108,108,193,0.08); border-left: 3px solid {ACCENT};
  padding: 8px 12px; border-radius: 4px; margin: 12px 0; page-break-inside: avoid; }}
.conclusion-label {{ color: {ACCENT}; font-weight: 600; font-size: 12px; margin-bottom: 2px; }}
.risk {{ margin-top: 22px; padding: 10px 14px; border: 1px solid {GRID}; border-radius: 6px; font-size: 11px;
  color: #4E5361; page-break-inside: avoid; }}
.risk .box-title {{ color: #4E5361; }}
.risk p {{ margin: 2px 0; }}
.sources {{ margin-top: 10px; color: {MUTED}; font-size: 11px; }}
.md p {{ margin: 8px 0; color: #3A3F4B; }}
.md li {{ margin: 4px 0; color: #3A3F4B; }}
.claim {{ margin: 12px 0; padding-left: 10px; border-left: 2px solid #D9D8F5; page-break-inside: avoid; }}
.claim-head {{ font-weight: 600; font-size: 13.5px; color: #1F2329; }}
.claim-body {{ margin: 2px 0 0; color: #4E5361; }}
.tally {{ color: {MUTED}; font-size: 11px; margin-top: 4px; }}
td, .kpi-value, .duel-val, .ent-fact-v {{ font-variant-numeric: tabular-nums; }}
.ent {{ margin: 12px 0 14px; page-break-inside: avoid; }}
.ent-title {{ font-weight: 600; font-size: 13px; margin-bottom: 6px; }}
.ent-name {{ font-weight: 600; font-size: 14px; }}
.ent-meta {{ margin-top: 2px; }}
.ent-kind {{ color: {MUTED}; font-size: 11px; margin-right: 6px; }}
.ent-tag {{ display: inline-block; font-size: 10px; line-height: 16px; padding: 0 5px; border-radius: 3px;
  background: #ECECFA; color: #4B4BA6; margin-right: 4px; }}
.ent-note {{ color: #4E5361; font-size: 12px; margin-top: 4px; }}
.ent-card {{ background: #F7F8FA; border-radius: 6px; padding: 10px 14px; }}
.ent-facts {{ display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 8px; }}
.ent-fact-v {{ font-size: 16px; font-weight: 600; }}
.ent-fact-l {{ color: {MUTED}; font-size: 11px; }}
.duel-head {{ display: flex; align-items: stretch; gap: 8px; position: relative; }}
.duel-side {{ flex: 1; background: #F7F8FA; border-radius: 6px; padding: 8px 12px; }}
.duel-side.side-1 {{ text-align: right; }}
.duel-vs {{ position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); background: #fff;
  color: {ACCENT}; font-weight: 700; font-size: 11px; border-radius: 50%; width: 26px; height: 26px;
  line-height: 26px; text-align: center; border: 1px solid #D9D8F5; }}
.duel-score {{ display: flex; justify-content: space-between; color: {MUTED}; font-size: 12px; margin: 8px 2px 4px; }}
.duel-score b {{ font-weight: 600; }}
.duel-score b.is-win {{ color: {ACCENT}; }}
.duel-row {{ display: flex; align-items: center; gap: 6px; padding: 4px 0; border-bottom: 1px solid #F1F2F5; }}
.duel-val {{ width: 64px; font-size: 12px; text-align: right; }}
.duel-val.v-1 {{ text-align: left; }}
.duel-val.is-win {{ font-weight: 600; color: #34347E; }}
.duel-track {{ flex: 1; height: 6px; background: #F1F2F5; border-radius: 3px; display: flex; }}
.duel-track.t-0 {{ justify-content: flex-end; }}
.duel-track i {{ display: block; height: 100%; border-radius: 3px; background: #C9CCD6; }}
.duel-track i.is-win {{ background: #8088DB; }}
.duel-label {{ width: 76px; text-align: center; color: #4E5361; font-size: 11px; }}
.ent-table th.ent-col {{ background: #F7F8FA; font-weight: 400; vertical-align: top; }}
.ent-table th.ent-row {{ background: none; color: #4E5361; font-weight: 400; white-space: nowrap; }}
.fit {{ margin: 12px 0; page-break-inside: avoid; }}
.fit-cols {{ display: flex; gap: 8px; }}
.fit-col {{ flex: 1; border-radius: 6px; padding: 8px 12px; }}
.fit-good {{ background: #EEF7F2; }}
.fit-bad {{ background: #FBF1F0; }}
.fit-label {{ font-weight: 600; font-size: 12px; margin-bottom: 2px; }}
.fit-good .fit-label {{ color: #1F7A55; }}
.fit-bad .fit-label {{ color: #A33A33; }}
.fit ul {{ margin: 0; padding-left: 16px; font-size: 12px; }}
"""


def _figure(block, label: str, inner: str) -> str:
    cap = f'<div class="fig-cap"><b>{esc(label)}</b>{esc(block.title)}</div>'
    src = f'<div class="fig-src">资料来源：{esc(block.source)}</div>' if block.source else ""
    kind = "is-chart" if block.type == "chart" else "is-table"
    return f'<div class="fig {kind}">{cap}{inner}{src}</div>'


def _entity_head(it) -> str:
    tags = "".join(f'<span class="ent-tag">{esc(t)}</span>' for t in it.tags)
    kind = f'<span class="ent-kind">{esc(it.kind)}</span>' if it.kind else ""
    note = f'<div class="ent-note">{_inline(it.note)}</div>' if it.note else ""
    return f'<div class="ent-name">{esc(it.name)}</div><div class="ent-meta">{kind}{tags}</div>{note}'


def _duel_rows(block, wins: set[tuple[int, int]]) -> str:
    """对决的逐项比较：左值 | 左条 | 指标 | 右条 | 右值；条长按这一项两边的绝对值归一。"""
    rows = []
    for mi, m in enumerate(block.metrics):
        nums = [cell_number(v) for v in m.values]
        scale = max((abs(n) for n in nums if n is not None), default=0) or 1.0

        def val(k: int) -> str:
            on = " is-win" if (mi, k) in wins else ""
            return f'<div class="duel-val v-{k}{on}">{esc(display_cell(m.values[k]))}</div>'

        def bar(k: int) -> str:
            on = ' class="is-win"' if (mi, k) in wins else ""
            width = 0 if nums[k] is None else abs(nums[k]) / scale * 100
            return f'<div class="duel-track t-{k}"><i{on} style="width:{width:.0f}%"></i></div>'

        rows.append(
            f'<div class="duel-row">{val(0)}{bar(0)}<div class="duel-label">{esc(m.label)}</div>{bar(1)}{val(1)}</div>'
        )
    return "".join(rows)


def entities_html(block) -> str:
    """对象卡：1 个画像 / 2 个对决 / 3–5 个并排。胜出格与「各赢几项」来自 entity_wins / tally。"""
    wins = {(mi, k) for mi, k in entity_wins(block)}
    n = len(block.items)
    cap = f'<div class="ent-title">{esc(block.title)}</div>' if block.title else ""
    src = f'<div class="fig-src">资料来源：{esc(block.source)}</div>' if block.source else ""
    if n == 1:
        it = block.items[0]
        facts = "".join(
            f'<div class="ent-fact"><div class="ent-fact-v">{esc(display_cell(m.values[0]))}</div>'
            f'<div class="ent-fact-l">{esc(m.label)}</div></div>'
            for m in block.metrics
        )
        return (
            f'<div class="ent is-one">{cap}<div class="ent-card">{_entity_head(it)}'
            + (f'<div class="ent-facts">{facts}</div>' if facts else "")
            + f"</div>{src}</div>"
        )
    t = tally(block)
    if n == 2:
        lead = ""
        if t:
            (a, ca), (b, cb) = t["wins"]
            ties = f'<span>持平 {t["ties"]}</span>' if t["ties"] else ""
            lead = (
                f'<div class="duel-score"><b class="{"is-win" if ca > cb else ""}">{esc(a)} 胜 {ca} 项</b>'
                f'{ties}<b class="{"is-win" if cb > ca else ""}">{esc(b)} 胜 {cb} 项</b></div>'
            )
        heads = "".join(f'<div class="duel-side side-{k}">{_entity_head(it)}</div>' for k, it in enumerate(block.items))
        return (
            f'<div class="ent is-duel">{cap}<div class="duel-head">{heads}<div class="duel-vs">VS</div></div>'
            f"{lead}{_duel_rows(block, wins)}{src}</div>"
        )
    head = "".join(f'<th class="ent-col">{_entity_head(it)}</th>' for it in block.items)
    body = []
    for mi, m in enumerate(block.metrics):
        cells = []
        for k, v in enumerate(m.values):
            inner = esc(display_cell(v))
            cells.append(f'<td><span class="best">{inner}</span></td>' if (mi, k) in wins else f"<td>{inner}</td>")
        cells = "".join(cells)
        body.append(f'<tr><th class="ent-row">{esc(m.label)}</th>{cells}</tr>')
    foot = f'<div class="tally">{esc(tally_text(t))}</div>' if t else ""
    return (
        f'<div class="ent is-grid">{cap}<div class="tbl-wrap"><table class="ent-table"><thead><tr><th></th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>{foot}{src}</div>'
    )


def fit_html(block) -> str:
    cols = []
    for cls, label, items in (("good", block.good_label, block.good), ("bad", block.bad_label, block.bad)):
        if items:
            lis = "".join(f"<li>{_inline(x)}</li>" for x in items)
            cols.append(f'<div class="fit-col fit-{cls}"><div class="fit-label">{esc(label)}</div><ul>{lis}</ul></div>')
    cap = f'<div class="ent-title">{esc(block.title)}</div>' if block.title else ""
    return f'<div class="fit">{cap}<div class="fit-cols">{"".join(cols)}</div></div>'


def block_html(block, label: str = "") -> str:
    if block.type == "entities":
        return entities_html(block)
    if block.type == "fit":
        return fit_html(block)
    if block.type == "markdown":
        return f'<div class="md">{markdown_to_html(block.text)}</div>'
    if block.type == "table":
        signed = {block.columns.index(c) for c in block.signed_columns}
        inner = _table_html(block.columns, block.rows, signed, best=best_cells(block))
        if t := tally(block):
            inner += f'<div class="tally">{esc(tally_text(t))}</div>'
        return _figure(block, label, inner)
    if block.type == "chart":
        return _figure(block, label, chart_svg(block))
    if block.type == "conclusion":
        return (
            '<div class="conclusion"><div class="conclusion-label">小结</div>'
            f"{markdown_to_html(block.text)}</div>"
        )
    return ""


TONE_LABELS = {"plus": "优势", "tradeoff": "权衡", "risk": "风险", "action": "建议"}


def kpi_class(kpi) -> str:
    if kpi.tone == "up":
        return "num-up"
    if kpi.tone == "down":
        return "num-down"
    return signed_class(kpi.value) if kpi.tone == "auto" else ""


def _head_html(report: Report) -> str:
    """结论条 + 指标卡 + 核心观点（带语气标记）。"""
    parts = []
    if report.verdict:
        parts.append(f'<div class="verdict"><div class="verdict-label">结论</div>{_inline(report.verdict)}</div>')
    if report.kpis:
        tiles = "".join(
            f'<div class="kpi"><div class="kpi-label">{esc(k.label)}</div>'
            f'<div class="kpi-value {kpi_class(k)}">{esc(tidy_text(k.value))}</div>'
            + (f'<div class="kpi-note">{esc(k.note)}</div>' if k.note else "")
            + "</div>"
            for k in report.kpis
        )
        parts.append(f'<div class="kpis n{len(report.kpis)}">{tiles}</div>')
    points = report.points()
    if points:
        items = "".join(
            "<li>"
            + (f'<span class="tone tone-{p.tone}">{TONE_LABELS[p.tone]}</span>' if p.tone in TONE_LABELS else "")
            + f"{_inline(p.text)}</li>"
            for p in points
        )
        parts.append(f'<div class="points"><div class="box-title">核心观点</div><ol>{items}</ol></div>')
    return "".join(parts)


def risk_paragraphs(report: Report, default: str) -> list[str]:
    return [t for t in (report.risk_note.strip(), default.strip()) if t]


def report_html(report: Report, *, risk_note_default: str = "", footer: str = "") -> str:
    """整份报告的独立 HTML（PDF 导出用）。骨架：报告头 → 核心观点 → 目录 → 章节 → 风险提示 → 来源。"""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.created_at))
    meta = [f"数据截至 {esc(report.as_of)}"] if report.as_of else []
    meta += [f"v{report.version}", f"生成于 {when}"]
    parts = [
        '<div class="cover"><div class="cover-kind">研究报告</div>'
        f"<h1>{esc(report.title)}</h1>"
        + ("" if report.verdict else f'<p class="cover-summary">{esc(report.summary)}</p>')
        + f'<div class="cover-meta">{"".join(f"<span>{m}</span>" for m in meta)}</div></div>'
    ]
    parts.append(_head_html(report))
    sections = numbered(report)
    titled = [h for h, _ in sections if h]
    if len(titled) >= TOC_MIN:
        parts.append(
            '<div class="toc"><div class="box-title">目录</div><ol>'
            + "".join(f"<li>{esc(h)}</li>" for h in titled)
            + "</ol></div>"
        )
    for (heading, items), sec in zip(sections, report.body_sections()):
        head = f'<h2 class="sec-title">{esc(heading)}</h2>' if heading else ""
        if getattr(sec, "lead", ""):
            head += f'<p class="sec-lead">{_inline(sec.lead)}</p>'
        body = "".join(block_html(b, label) for b, label in items)
        parts.append(f'<div class="sec">{head}{body}</div>')
    risk = risk_paragraphs(report, risk_note_default)
    if risk:
        parts.append(
            '<div class="risk"><div class="box-title">风险提示</div>'
            + "".join(f"<p>{esc(t)}</p>" for t in risk)
            + "</div>"
        )
    if report.sources:
        parts.append('<div class="sources">数据来源：' + "；".join(esc(x) for x in report.sources) + "</div>")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{esc(report.title)}</title><style>{_pdf_css(report.title, footer)}</style></head>"
        f"<body>{''.join(parts)}</body></html>"
    )
