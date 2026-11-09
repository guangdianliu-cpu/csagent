"""将语义查询返回的管道分隔表格解析为结构化行。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def parse_pipe_table(text: str) -> list[dict[str, Any]] | None:
    """解析管道表；支持前言行 + markdown `| col |` 格式。

    兼容两种表头：
    - `col1 | col2`
    - `| col1 | col2 |`
    无法识别时返回 None。
    """
    if not text or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith(("QuerySpec", "权限错误", "未知实体", "未找到", "业务")):
        return None

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    # ---------- 跳过「查询成功…」等前言，定位表头 ----------
    header_idx = _find_header_line_index(lines)
    if header_idx is None:
        return None

    columns = _split_pipe_cells(lines[header_idx])
    if not columns or any(not col for col in columns):
        return None

    rows: list[dict[str, Any]] = []
    for line in lines[header_idx + 1 :]:
        if " | " not in line and not line.startswith("|"):
            continue
        values = _split_pipe_cells(line)
        if not values:
            continue
        # markdown 分隔行：| --- | --- |
        if all(v.replace("-", "").strip() == "" for v in values):
            continue
        if len(values) != len(columns):
            continue
        rows.append(
            {column: _coerce_cell(column, value) for column, value in zip(columns, values)}
        )
    return rows or None


def _find_header_line_index(lines: list[str]) -> int | None:
    """找到首个可作为表头的管道行（含 `|` 分隔且非整行分隔符）。"""
    for idx, line in enumerate(lines):
        if " | " not in line and not (line.startswith("|") and line.count("|") >= 2):
            continue
        cells = _split_pipe_cells(line)
        if not cells:
            continue
        if all(c.replace("-", "").strip() == "" for c in cells):
            continue
        # 表头至少两列，且不全是纯数字（避免误把数据行当表头）
        if len(cells) < 2:
            continue
        return idx
    return None


def _split_pipe_cells(line: str) -> list[str]:
    """按 `|` 拆单元格，去掉首尾空单元格（markdown 外层竖线）。"""
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def _coerce_cell(column: str, value: str) -> Any:
    if value in {"", "None", "null"}:
        return None
    col_lower = column.lower()
    if col_lower.endswith("_code") or col_lower in {
        "fund_code",
        "product_code",
        "trade_acco",
        "custno",
        "mem_no",
        "member_id",
    }:
        return value
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in value or "e" in lowered:
            return float(Decimal(value))
        return int(value)
    except (InvalidOperation, ValueError):
        return value
