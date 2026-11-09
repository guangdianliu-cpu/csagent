"""Excel 结构化读写工具包（openpyxl）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from csagent.channels.workspace_uploads import bind_session_workspace_path
from csagent.infra.config import PROJECT_ROOT
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.packs.workspace_files_pack import (
    _DEFAULT_DENY_NAMES,
    is_denied_path,
    resolve_workspace_path,
)

logger = structlog.get_logger(__name__)


def _require_openpyxl():
    try:
        import openpyxl  # type: ignore

        return openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Excel 能力需要 openpyxl：pip install 'cs-agent[docs]' 或 pip install openpyxl"
        ) from exc


def _col_to_index(col: Any) -> int:
    """1-based 列号；支持 'A'/'B' 或整数。"""
    if isinstance(col, int):
        return max(1, col)
    text = str(col or "").strip().upper()
    if text.isdigit():
        return max(1, int(text))
    if not text.isalpha():
        raise ValueError(f"非法列标识: {col!r}")
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class ExcelTool(BaseTool):
    """Excel 操作：list_sheets / read / write / filter / create。"""

    name = "excel_tool"
    description = (
        "操作 Excel（.xlsx/.xlsm）。参数 action 必填："
        "list_sheets|read|write|filter|create；path 默认相对当前会话 "
        "data/uploads/{session}/。"
        "read: sheet?, start_row?, max_rows?；"
        "write: sheet?, cells=[{row,col,value}]；"
        "filter: sheet?, column(表头名或列号), op=eq|contains|ne, value, max_rows?；"
        "create: sheet?, headers?, rows?。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "list_sheets | read | write | filter | create",
                },
                "path": {"type": "string", "description": "Excel 文件路径"},
                "sheet": {"type": "string", "description": "工作表名"},
                "start_row": {"type": "integer"},
                "max_rows": {"type": "integer"},
                "cells": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "column": {"description": "表头名或列号"},
                "op": {"type": "string", "description": "eq | contains | ne"},
                "value": {"description": "筛选值"},
                "headers": {"type": "array", "items": {"type": "string"}},
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                },
            },
            "required": ["action", "path"],
            "additionalProperties": True,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        max_rows: int = 100,
        deny_names: set[str] | None = None,
        session_scoped: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._max_rows = max(1, min(max_rows, 500))
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._session_scoped = session_scoped

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        action = str(arguments.get("action") or "").strip().lower()
        path_arg = str(arguments.get("path") or "").strip()
        if not action:
            return "错误: 缺少 action（list_sheets|read|write|filter|create）"
        if not path_arg:
            return "错误: 缺少 path"

        for_write = action in {"write", "create"}
        if self._session_scoped:
            path_arg = bind_session_workspace_path(
                path_arg,
                scope.session_id,
                for_write=for_write,
                project_root=self._root,
            )
            if any(part == ".." for part in Path(path_arg.replace("\\", "/")).parts):
                return "错误: 路径不得包含 .."

        target = resolve_workspace_path(
            self._root, path_arg, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if is_denied_path(target, self._deny_names):
            return f"错误: 拒绝操作敏感文件: {target.name}"

        try:
            openpyxl = _require_openpyxl()
        except RuntimeError as exc:
            return str(exc)

        try:
            if action == "list_sheets":
                return self._list_sheets(openpyxl, target)
            if action == "read":
                return self._read(openpyxl, target, arguments)
            if action == "write":
                return self._write(openpyxl, target, arguments)
            if action == "filter":
                return self._filter(openpyxl, target, arguments)
            if action == "create":
                return self._create(openpyxl, target, arguments)
            return f"错误: 不支持的 action={action!r}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("excel_tool_failed", action=action, path=path_arg, error=str(exc))
            return f"Excel 操作失败: {exc}"

    def _list_sheets(self, openpyxl: Any, path: Path) -> str:
        if not path.is_file():
            return f"错误: 文件不存在: {path.name}"
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            names = wb.sheetnames
        finally:
            wb.close()
        rel = path.relative_to(self._root.resolve()).as_posix()
        lines = [f"文件: {rel}", f"工作表({len(names)}):"]
        lines.extend(f"- {n}" for n in names)
        return "\n".join(lines)

    def _pick_sheet(self, wb: Any, arguments: dict[str, Any]) -> Any:
        name = str(arguments.get("sheet") or "").strip()
        if name:
            if name not in wb.sheetnames:
                raise ValueError(f"工作表不存在: {name}；可选: {wb.sheetnames}")
            return wb[name]
        return wb.active

    def _read(self, openpyxl: Any, path: Path, arguments: dict[str, Any]) -> str:
        if not path.is_file():
            return f"错误: 文件不存在: {path.name}"
        start_row = max(1, int(arguments.get("start_row") or 1))
        try:
            max_rows = int(arguments.get("max_rows") or self._max_rows)
        except (TypeError, ValueError):
            max_rows = self._max_rows
        max_rows = max(1, min(max_rows, 500))

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            ws = self._pick_sheet(wb, arguments)
            lines = [f"Sheet: {ws.title}", f"从第 {start_row} 行起，最多 {max_rows} 行:"]
            count = 0
            for i, row in enumerate(ws.iter_rows(min_row=start_row, values_only=True), start=start_row):
                cells = [_cell_str(c) for c in row]
                if not any(cells):
                    continue
                lines.append(f"R{i}\t" + "\t".join(cells))
                count += 1
                if count >= max_rows:
                    break
            if count == 0:
                lines.append("(无数据行)")
            return "\n".join(lines)
        finally:
            wb.close()

    def _write(self, openpyxl: Any, path: Path, arguments: dict[str, Any]) -> str:
        cells = arguments.get("cells")
        if not isinstance(cells, list) or not cells:
            return "错误: write 需要 cells 列表，如 [{row:1,col:1,value:'x'}]"

        if path.exists():
            wb = openpyxl.load_workbook(str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            wb = openpyxl.Workbook()
        try:
            sheet_name = str(arguments.get("sheet") or "").strip()
            if sheet_name:
                ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.create_sheet(sheet_name)
            else:
                ws = wb.active
            written = 0
            for item in cells:
                if not isinstance(item, dict):
                    continue
                row = int(item.get("row") or 0)
                if row < 1:
                    continue
                col = _col_to_index(item.get("col") or 1)
                ws.cell(row=row, column=col, value=item.get("value"))
                written += 1
            wb.save(str(path))
            rel = path.relative_to(self._root.resolve()).as_posix()
            hint = ""
            if "data/uploads/" in rel.replace("\\", "/"):
                hint = f'\n若用户要发回文件，请调用 send_file({{"path":"{rel}"}})。'
            return f"已写入 {written} 个单元格 → {rel}（sheet={ws.title}）{hint}"
        finally:
            wb.close()

    def _filter(self, openpyxl: Any, path: Path, arguments: dict[str, Any]) -> str:
        if not path.is_file():
            return f"错误: 文件不存在: {path.name}"
        column = arguments.get("column")
        if column is None or str(column).strip() == "":
            return "错误: filter 需要 column（表头名或列号）"
        op = str(arguments.get("op") or "eq").strip().lower()
        if op not in {"eq", "contains", "ne"}:
            return "错误: op 仅支持 eq / contains / ne"
        value = _cell_str(arguments.get("value"))
        try:
            max_rows = int(arguments.get("max_rows") or self._max_rows)
        except (TypeError, ValueError):
            max_rows = self._max_rows
        max_rows = max(1, min(max_rows, 500))

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            ws = self._pick_sheet(wb, arguments)
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header = next(rows_iter)
            except StopIteration:
                return "错误: 工作表为空"
            header_cells = [_cell_str(c) for c in header]
            col_label = str(column).strip()
            col_idx = 0
            for i, h in enumerate(header_cells, start=1):
                if h == col_label or h.strip() == col_label:
                    col_idx = i
                    break
            if col_idx < 1:
                try:
                    col_idx = _col_to_index(column)
                except ValueError:
                    return f"错误: 找不到列 {column!r}；表头: {header_cells}"
            if col_idx < 1:
                return f"错误: 找不到列 {column!r}；表头: {header_cells}"

            lines = [
                f"Sheet: {ws.title}",
                f"筛选: 列{col_idx}({header_cells[col_idx - 1] if col_idx <= len(header_cells) else '?'}) "
                f"{op} {value!r}",
                "R1\t" + "\t".join(header_cells),
            ]
            matched = 0
            for r_i, row in enumerate(rows_iter, start=2):
                cells = [_cell_str(c) for c in row]
                cell_val = cells[col_idx - 1] if col_idx <= len(cells) else ""
                ok = False
                if op == "eq":
                    ok = cell_val == value
                elif op == "ne":
                    ok = cell_val != value
                elif op == "contains":
                    ok = value in cell_val
                if not ok:
                    continue
                lines.append(f"R{r_i}\t" + "\t".join(cells))
                matched += 1
                if matched >= max_rows:
                    lines.append(f"... 已截断，最多 {max_rows} 行")
                    break
            if matched == 0:
                lines.append("(无匹配行)")
            else:
                lines.insert(2, f"匹配 {matched} 行")
            return "\n".join(lines)
        finally:
            wb.close()

    def _create(self, openpyxl: Any, path: Path, arguments: dict[str, Any]) -> str:
        if path.suffix.lower() not in {".xlsx", ".xlsm"}:
            return "错误: create 仅支持 .xlsx / .xlsm 后缀"
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        try:
            ws = wb.active
            sheet_name = str(arguments.get("sheet") or "").strip()
            if sheet_name:
                ws.title = sheet_name[:31]
            headers = arguments.get("headers") or []
            rows = arguments.get("rows") or []
            if not (isinstance(headers, list) and headers) and not (
                isinstance(rows, list) and rows
            ):
                return (
                    "错误: create 须提供 headers 和/或 rows，"
                    "禁止创建空表；产品数据须来自上游 product 查询结果。"
                )
            r = 1
            if isinstance(headers, list) and headers:
                for c, val in enumerate(headers, start=1):
                    ws.cell(row=1, column=c, value=val)
                r = 2
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, (list, tuple)):
                        continue
                    for c, val in enumerate(row, start=1):
                        ws.cell(row=r, column=c, value=val)
                    r += 1
            if r <= 1:
                return "错误: create 未写入任何有效行，请检查 headers/rows 格式"
            wb.save(str(path))
            rel = path.relative_to(self._root.resolve()).as_posix()
            hint = ""
            if "data/uploads/" in rel.replace("\\", "/"):
                hint = f'\n可用 send_file({{"path":"{rel}"}}) 发回用户。'
            return f"已创建 Excel: {rel}（{r - 1} 行含表头）{hint}"
        finally:
            wb.close()



class ExcelToolPack(ToolPack):
    pack_name = "excel_tools"
    description = "Excel 结构化读写/筛选"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        root = Path(str(config.get("root_dir") or "."))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root = root.resolve()
        session_scoped = bool(config.get("session_scoped", True))
        allow_absolute = bool(config.get("allow_absolute", False))
        excel = ExcelTool(
            root=root,
            allow_absolute=allow_absolute,
            max_rows=int(config.get("max_rows") or 100),
            session_scoped=session_scoped,
        )
        for item in config.get("tools") or []:
            if isinstance(item, dict) and item.get("name") == "excel_tool" and item.get("description"):
                excel.description = str(item["description"])
        self._tools = [excel]
        logger.info(
            "excel_pack_ready",
            root=str(root),
            tools=[t.name for t in self._tools],
            session_scoped=session_scoped,
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
