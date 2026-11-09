"""把利得基金投研 Excel 数据库转成本地 SQLite 缓存，并负责「发布」新数据。

- 覆盖 19 张核心与扩展投研数据表（涵盖公募纯债/权益/固收+、QDII、基金经理、交易能力、
  可转债投资、量化策略、归因评分、行业轮动、配置工具、基金公司、私募基金等全谱系）。
- 源文件目录：环境变量 ``FUND_RESEARCH_DATA_DIR``，默认 ``<项目根>/data/fund-research/``。
  数据文件不进 git 仓库（体积大、属内部数据）。
- 输出：<skill>/data/fund_research.sqlite3

更新语义（只发布、不保留历史）：
- 查询侧**不再**因源文件变新而现场重建；只有缓存库完全不存在时才全量构建一次。
- 新数据走 ``--publish <文件>``：按文件名匹配到它负责的表 → 校验 sheet →
  在缓存库的**副本**里只重建这些表 → 原子替换线上库 → 源文件放进数据目录并删掉旧版。
  任何一步失败，线上库和数据目录都保持原样。
- 全量重建 ``--force`` 同样先写临时文件再原子替换，不会出现「半张库」。
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import shutil
import sqlite3
import time as _time
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parent.parent
_DB_PATH = _SKILL_ROOT / "data" / "fund_research.sqlite3"


def data_dir() -> Path:
    """源 Excel 所在目录。每次调用都读环境变量，便于测试与部署覆盖。"""
    raw = (os.getenv("FUND_RESEARCH_DATA_DIR") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else _PROJECT_ROOT / path
    return _PROJECT_ROOT / "data" / "fund-research"

# 分组标题 → 列名消歧前缀
_GROUP_PREFIX = {
    "大类主题配置数据": "主题占比",
    "行业配置数据": "行业占比",
}

# 19 张表的全量配置清单
TABLE_CONFIGS: list[dict[str, Any]] = [
    {
        "table": "active_bond_funds",
        "file_pattern": "【利得基金】公募基金数据库*.xlsx",
        "sheet": "主动纯债基金数据库",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "active_equity_funds",
        "file_pattern": "【利得基金】公募基金数据库*.xlsx",
        "sheet": "主动权益基金数据库",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "active_fixed_income_plus_funds",
        "file_pattern": "【利得基金】公募基金数据库*.xlsx",
        "sheet": "主动固收+基金数据库",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "qdii_equity_funds",
        "file_pattern": "【利得基金】QDII基金数据库*.xlsx",
        "sheet": "QDII权益及另类型基金",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "qdii_bond_funds",
        "file_pattern": "【利得基金】QDII基金数据库*.xlsx",
        "sheet": "QDII债券型基金",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "fund_managers_equity",
        "file_pattern": "【利得基金】主动基金经理数据库*.xlsx",
        "sheet": "主动权益基金经理数据库",
        "pk": None,
        "indexes": ["基金经理", "基金公司"],
        "header_depth": 2,
    },
    {
        "table": "fund_managers_fixed_income",
        "file_pattern": "【利得基金】主动基金经理数据库*.xlsx",
        "sheet": "主动固收+基金经理数据库",
        "pk": None,
        "indexes": ["基金经理", "基金公司"],
        "header_depth": 2,
    },
    {
        "table": "fund_trading_ability",
        "file_pattern": "【利得基金】主动权益基金交易能力评价*.xlsx",
        "sheet": "交易能力",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "fund_companies",
        "file_pattern": "【利得基金】公募基金公司数据库*.xlsx",
        "sheet": "公募基金管理人评估表",
        "pk": "基金管理人",
        "header_depth": 3,
    },
    {
        "table": "convertible_bond_funds",
        "file_pattern": "【利得基金】公募基金可转债投资能力与业绩归因数据库*.xlsx",
        # 2026 新版 sheet 名带「基金」前缀；旧版曾为「转债投资能力数据库」
        "sheet": "基金转债投资能力数据库",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "quant_equity_funds",
        "file_pattern": "【利得基金】公募量化策略权益基金的分析与评价*.xlsx",
        "sheet": "量化策略权益基金",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "attribution_equity",
        "file_pattern": "【利得基金】基金经理业绩归因的分析与评价*.xlsx",
        "sheet": "主动权益基金业绩归因评分",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "attribution_fixed_income",
        "file_pattern": "【利得基金】基金经理业绩归因的分析与评价*.xlsx",
        "sheet": "主动固收+基金业绩归因评分",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "industry_rotation_equity",
        "file_pattern": "【利得基金】基金经理主题及行业轮动的分析与优选*.xlsx",
        "sheet": "主动权益基金经理行业轮动分析与优选",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "industry_rotation_fixed_income",
        "file_pattern": "【利得基金】基金经理主题及行业轮动的分析与优选*.xlsx",
        "sheet": "主动固收+基金经理行业轮动分析与优选",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "industry_allocation_sw",
        "file_pattern": "【利得基金】中信及申万行业基金配置工具库(净值比)*.xlsx",
        "sheet": "申万行业",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "industry_allocation_zx",
        "file_pattern": "【利得基金】中信及申万行业基金配置工具库(净值比)*.xlsx",
        "sheet": "中信行业",
        "pk": "基金代码",
        "header_depth": 2,
    },
    {
        "table": "manager_industry_capabilities",
        "file_pattern": "【利得基金】基金经理主题及行业投资能力数据库*.xlsx",
        "sheet": "基金经理主题及行业投资能力数据库",
        "pk": "基金代码",
        "header_depth": 3,
    },
    {
        "table": "private_funds",
        "file_pattern": "【利得基金】私募基金数据库*.xlsx",
        "sheet": "私募基金数据库",
        "pk": "产品代码",
        "header_depth": 2,
    },
]


def _find_source_file(pattern: str) -> Path:
    candidates = sorted(glob.glob(str(data_dir() / pattern)), key=lambda p: Path(p).stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"未在 {data_dir()} 找到匹配源文件: {pattern}")
    return Path(candidates[-1])


def source_patterns() -> list[str]:
    """去重后的源文件名模式，顺序与 TABLE_CONFIGS 一致。"""
    seen: list[str] = []
    for cfg in TABLE_CONFIGS:
        if cfg["file_pattern"] not in seen:
            seen.append(cfg["file_pattern"])
    return seen


def match_pattern(filename: str) -> str | None:
    """上传文件名 → 它对应的源文件模式；都不匹配返回 None。"""
    for pattern in source_patterns():
        if fnmatch.fnmatchcase(filename, pattern):
            return pattern
    return None


def _cell_text(cell: Any) -> str:
    """单元格转列名片段。

    归因表分组行常把中文拆成「股 票 资 产 配 置」（字间空格或换行），
    入库时压掉全部空白，得到「股票资产配置」，才能和方法论文档对上。
    """
    if cell is None:
        return ""
    return "".join(str(cell).split())


def _forward_fill(row: tuple[Any, ...]) -> list[str]:
    filled: list[str] = []
    last = ""
    for cell in row:
        text = _cell_text(cell)
        if text:
            last = text
        filled.append(last)
    return filled


def _resolve_headers_2(group_row: tuple[Any, ...], field_row: tuple[Any, ...], table_name: str) -> list[str]:
    group_names = _forward_fill(group_row)
    field_names = [_cell_text(c) for c in field_row]

    # 特殊优化：对于私募基金表，区别产品成立时间与管理人成立时间
    if table_name == "private_funds":
        for i, (g, f) in enumerate(zip(group_names, field_names)):
            if f == "成立时间" and "管理人" in g:
                field_names[i] = "管理人_成立时间"

    freq: dict[str, int] = {}
    for name in field_names:
        if name:
            freq[name] = freq.get(name, 0) + 1

    candidates: list[str] = []
    for field, group in zip(field_names, group_names):
        if not field:
            candidates.append(group or "unnamed")
            continue
        if group in _GROUP_PREFIX:
            candidates.append(f"{_GROUP_PREFIX[group]}_{field}")
        # 兼容旧版：老三表近一年评分保持不带前缀，近三年评分带前缀
        elif table_name in ("active_bond_funds", "active_equity_funds", "active_fixed_income_plus_funds") and "近一年利得评分" in group:
            candidates.append(field)
        elif table_name in ("active_bond_funds", "active_equity_funds", "active_fixed_income_plus_funds") and "近三年利得评分" in group:
            candidates.append(f"近三年_{field}")
        elif freq.get(field, 0) > 1 and group and group != field:
            clean_group = group.split("\n")[0].strip()
            candidates.append(f"{clean_group}_{field}")
        else:
            candidates.append(field)

    seen: dict[str, int] = {}
    result: list[str] = []
    for c in candidates:
        if c not in seen:
            seen[c] = 1
            result.append(c)
        else:
            seen[c] += 1
            result.append(f"{c}_{seen[c]}")
    return result


def _resolve_headers_3(row0: tuple[Any, ...], row1: tuple[Any, ...], row2: tuple[Any, ...], table_name: str) -> list[str]:
    if table_name == "fund_companies":
        fields = [_cell_text(c) for c in row2]
        candidates = []
        last_metric = ""
        for f in fields:
            if not f:
                candidates.append("unnamed")
            elif f == "得分" and last_metric:
                clean_metric = last_metric.split("(")[0].strip()
                candidates.append(f"{clean_metric}_得分")
            else:
                candidates.append(f)
                last_metric = f
        seen: dict[str, int] = {}
        res = []
        for c in candidates:
            seen[c] = seen.get(c, 0) + 1
            res.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
        return res

    if table_name == "manager_industry_capabilities":
        g0 = _forward_fill(row0)
        g1 = _forward_fill(row1)
        f2 = [_cell_text(c) for c in row2]
        candidates = []
        for i, (a, b, c) in enumerate(zip(g0, g1, f2)):
            if not c:
                candidates.append(f"col_{i}")
                continue
            if i < 12:  # 前 12 列为基础信息
                candidates.append(c)
            else:
                clean_a = a.split("\n")[0].split("(")[0].strip()
                parts = [p for p in [clean_a, b, c] if p]
                candidates.append("_".join(parts))
        seen = {}
        res = []
        for c in candidates:
            seen[c] = seen.get(c, 0) + 1
            res.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
        return res

    return _resolve_headers_2(row0, row2, table_name)


def _coerce_cell(value: Any) -> Any:
    """把 openpyxl 单元格值收成 sqlite 可绑定类型。

    Excel 偶发给出 datetime.time（纯时间）或 datetime；sqlite3 不认 time，
    也不认未注册的自定义类型，统一落成 ISO 文本。
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return None if stripped in ("", "-", "—", "none", "None", "null") else stripped
    # datetime 是 date 子类，必须先于 date 判断
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec="seconds")
    return value


def _sniff_column_types(rows: list[list[Any]], n_cols: int) -> list[str]:
    types: list[str] = []
    for col_idx in range(n_cols):
        numeric = 0
        total = 0
        for row in rows:
            if col_idx >= len(row):
                continue
            v = row[col_idx]
            if v is None:
                continue
            total += 1
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric += 1
            elif isinstance(v, str):
                try:
                    float(v)
                    numeric += 1
                except ValueError:
                    pass
        types.append("REAL" if total and numeric == total else "TEXT")
    return types


def _load_sheet(ws: Any, depth: int, table_name: str) -> tuple[list[str], list[list[Any]]]:
    rows_iter = ws.iter_rows(values_only=True)
    r0 = next(rows_iter)
    r1 = next(rows_iter)
    if depth == 3:
        r2 = next(rows_iter)
        headers = _resolve_headers_3(r0, r1, r2, table_name)
    else:
        headers = _resolve_headers_2(r0, r1, table_name)

    data_rows: list[list[Any]] = []
    for row in rows_iter:
        if row[0] is None and (len(row) < 2 or row[1] is None):
            continue  # 空行跳过
        data_rows.append([_coerce_cell(v) for v in row[:len(headers)]])
    return headers, data_rows


_META_DDL = (
    "CREATE TABLE IF NOT EXISTS _meta (source_file TEXT, source_mtime REAL, "
    "built_at TEXT, table_name TEXT, row_count INTEGER, col_count INTEGER)"
)


class PublishError(RuntimeError):
    """发布被拒（文件名不匹配、缺 sheet、空表等）。消息直接给到管理页面。"""


def _tmp_db_path() -> Path:
    """与线上库同目录的临时文件：同一文件系统，``os.replace`` 才是原子的。"""
    return _DB_PATH.with_name(f".{_DB_PATH.stem}.{os.getpid()}.{int(_time.time() * 1000)}.tmp")


def _atomic_replace(src: Path, dst: Path) -> None:
    """原子替换。Windows 上目标被其它进程打开时会拒绝，短暂重试几次。"""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            _time.sleep(0.5)


def _build_tables(
    conn: sqlite3.Connection,
    cfgs: list[dict[str, Any]],
    file_for: dict[str, Path],
    *,
    verbose: bool,
    strict: bool,
) -> list[dict[str, Any]]:
    """在 ``conn`` 里（重）建 ``cfgs`` 列出的表，返回每张表的行列数。

    :param file_for: file_pattern → 源文件
    :param strict: True 时缺 sheet / 空表直接抛 PublishError（发布路径）；
        False 时沿用旧行为只告警跳过（全量构建路径）。
    """
    import openpyxl

    conn.execute(_META_DDL)
    report: list[dict[str, Any]] = []
    opened_wbs: dict[str, Any] = {}
    try:
        for cfg in cfgs:
            tname = cfg["table"]
            sname = cfg["sheet"]
            src_file = file_for[cfg["file_pattern"]]
            if src_file.name not in opened_wbs:
                if verbose:
                    print(f"  加载工作簿: {src_file.name}")
                opened_wbs[src_file.name] = openpyxl.load_workbook(str(src_file), read_only=True, data_only=True)
            wb = opened_wbs[src_file.name]

            if sname not in wb.sheetnames:
                if strict:
                    raise PublishError(
                        f"{src_file.name} 缺少 sheet「{sname}」（现有：{', '.join(wb.sheetnames)}）"
                    )
                if verbose:
                    print(f"  [警告] sheet 不存在，跳过: {sname} (file={src_file.name})")
                continue

            headers, rows = _load_sheet(wb[sname], cfg["header_depth"], tname)
            if strict and not rows:
                raise PublishError(f"{src_file.name} 的 sheet「{sname}」没有数据行")
            col_types = _sniff_column_types(rows, len(headers))
            pk = cfg["pk"]

            conn.execute(f'DROP TABLE IF EXISTS "{tname}"')
            col_defs = ", ".join(
                f'"{h}" {t}{" PRIMARY KEY" if pk and h == pk else ""}'
                for h, t in zip(headers, col_types)
            )
            conn.execute(f'CREATE TABLE "{tname}" ({col_defs})')
            placeholders = ", ".join("?" for _ in headers)
            conn.executemany(
                f'INSERT INTO "{tname}" VALUES ({placeholders})',
                (row[:len(headers)] for row in rows),
            )
            for idx_col in cfg.get("indexes") or []:
                if idx_col in headers:
                    conn.execute(f'CREATE INDEX "idx_{tname}_{idx_col}" ON "{tname}" ("{idx_col}")')

            conn.execute('DELETE FROM _meta WHERE table_name = ?', (tname,))
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, ?)",
                (src_file.name, src_file.stat().st_mtime, datetime.now().isoformat(timespec="seconds"),
                 tname, len(rows), len(headers)),
            )
            report.append({"table": tname, "sheet": sname, "rows": len(rows), "cols": len(headers)})
            if verbose:
                print(f"  [完成] {tname:<32}: {len(rows):>5} 行 / {len(headers):>3} 列 (PK={pk})")
    finally:
        for wb in opened_wbs.values():
            wb.close()
    return report


def build(*, force: bool = False, verbose: bool = True) -> Path:
    """全量构建。库已存在且未 force 时直接返回（不再按 mtime 自动重建）。"""
    if not force and _DB_PATH.is_file():
        if verbose:
            print(f"缓存库已存在，跳过（需要全量重建请加 --force）: {_DB_PATH.name}")
        return _DB_PATH

    sources = {pattern: _find_source_file(pattern) for pattern in source_patterns()}
    if verbose:
        print(f"开始全量重建投研缓存库 ({len(TABLE_CONFIGS)} 张表) -> {_DB_PATH.name}")

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_db_path()
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            _build_tables(conn, TABLE_CONFIGS, sources, verbose=verbose, strict=False)
            conn.commit()
        finally:
            conn.close()
        _atomic_replace(tmp, _DB_PATH)
    finally:
        tmp.unlink(missing_ok=True)

    if verbose:
        print(f"\n全部 {len(TABLE_CONFIGS)} 张表缓存构建完毕: {_DB_PATH}")
    return _DB_PATH


def publish(upload: Path, *, filename: str | None = None, verbose: bool = False) -> dict[str, Any]:
    """发布一份新的源文件：只重建它负责的表，成功后替换线上库与数据目录里的旧文件。

    :param upload: 已落到磁盘的上传文件（任意临时路径）
    :param filename: 原始文件名；缺省取 ``upload.name``。用它匹配 file_pattern。
    :returns: {"file", "pattern", "tables": [...], "replaced": [...]}
    """
    name = Path(filename or upload.name).name
    pattern = match_pattern(name)
    if pattern is None:
        raise PublishError(
            f"文件名「{name}」不属于任何已知数据集。可接受的文件名模式：" + "；".join(source_patterns())
        )
    cfgs = [c for c in TABLE_CONFIGS if c["file_pattern"] == pattern]

    # 先把文件改名放到数据目录旁的暂存位置：_meta 里记录的文件名要是正式名字
    target_dir = data_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / f".uploading-{name}"
    shutil.copyfile(upload, staged)

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_db_path()
    try:
        if _DB_PATH.is_file():
            # 在线上库的副本上改，读侧全程看到的是旧库
            src = sqlite3.connect(str(_DB_PATH))
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        conn = sqlite3.connect(str(tmp))
        try:
            final_path = target_dir / name
            report = _build_tables(conn, cfgs, {pattern: staged}, verbose=verbose, strict=True)
            # _meta 里记正式文件名，而不是暂存名
            conn.execute(
                "UPDATE _meta SET source_file = ? WHERE source_file = ?", (final_path.name, staged.name)
            )
            conn.commit()
        finally:
            conn.close()

        # 旧版源文件先删（同模式、不同名），再把新文件转正，最后切库
        replaced: list[str] = []
        for old in glob.glob(str(target_dir / pattern)):
            old_path = Path(old)
            if old_path.name != name:
                old_path.unlink(missing_ok=True)
                replaced.append(old_path.name)
        os.replace(staged, final_path)
        _atomic_replace(tmp, _DB_PATH)
    except PublishError as exc:
        staged.unlink(missing_ok=True)
        raise PublishError(str(exc).replace(staged.name, name)) from None
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)

    return {"file": name, "pattern": pattern, "tables": report, "replaced": replaced}


def describe() -> dict[str, Any]:
    """当前数据状态：每个源文件模式对应的文件、表与行数。供管理页面展示。"""
    meta: dict[str, dict[str, Any]] = {}
    if _DB_PATH.is_file():
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        try:
            for row in conn.execute(
                "SELECT table_name, source_file, built_at, row_count, col_count FROM _meta"
            ):
                meta[row[0]] = {
                    "source_file": row[1], "built_at": row[2], "rows": row[3], "cols": row[4],
                }
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    datasets = []
    for pattern in source_patterns():
        files = sorted(glob.glob(str(data_dir() / pattern)))
        current = Path(files[-1]) if files else None
        tables = []
        for cfg in TABLE_CONFIGS:
            if cfg["file_pattern"] != pattern:
                continue
            m = meta.get(cfg["table"], {})
            tables.append({
                "table": cfg["table"], "sheet": cfg["sheet"],
                "rows": m.get("rows"), "built_at": m.get("built_at"),
                "source_file": m.get("source_file"),
            })
        datasets.append({
            "pattern": pattern,
            "file": current.name if current else None,
            "size": current.stat().st_size if current else None,
            "modified_at": (
                datetime.fromtimestamp(current.stat().st_mtime).isoformat(timespec="seconds")
                if current else None
            ),
            "tables": tables,
        })
    return {
        "data_dir": str(data_dir()),
        "db_exists": _DB_PATH.is_file(),
        "datasets": datasets,
    }


def ensure_cache_fresh() -> Path:
    """查询前调用：只保证缓存库存在，不因源文件变新而现场重建。"""
    return build(force=False, verbose=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="从数据目录全量重建")
    parser.add_argument("--publish", metavar="FILE", help="发布一份新源文件（只重建它负责的表）")
    parser.add_argument("--filename", help="与 --publish 连用：用于匹配数据集的原始文件名")
    parser.add_argument("--describe", action="store_true", help="以 JSON 输出当前数据状态")
    parser.add_argument("--json", action="store_true", help="结果以 JSON 输出（供服务端调用）")
    args = parser.parse_args()

    if args.describe:
        print(json.dumps(describe(), ensure_ascii=False))
        return 0

    if args.publish:
        try:
            result = publish(Path(args.publish), filename=args.filename, verbose=not args.json)
        except PublishError as exc:
            if args.json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"发布被拒: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            if args.json:
                print(json.dumps({"ok": False, "error": f"构建失败: {exc}"}, ensure_ascii=False))
            else:
                import traceback
                traceback.print_exc()
            return 1
        if args.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        else:
            print(f"发布完成: {result['file']} -> {[t['table'] for t in result['tables']]}")
        return 0

    try:
        path = build(force=args.force, verbose=True)
    except Exception as exc:  # noqa: BLE001
        print(f"构建失败: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print(f"完成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
