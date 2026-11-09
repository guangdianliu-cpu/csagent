"""投研数据发布（skills/fund-research-db/scripts/build_cache.py）。

用小型临时 Excel 验证：只重建上传文件负责的表、旧版源文件被替换、
校验失败时线上库与数据目录都不动、查询侧不再因源文件变新而现场重建。
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import openpyxl
import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "fund-research-db" / "scripts"


@pytest.fixture
def bc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import build_cache

    build_cache = importlib.reload(build_cache)
    monkeypatch.setenv("FUND_RESEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(build_cache, "_DB_PATH", tmp_path / "db" / "fund_research.sqlite3")
    # 只保留两张表，避免测试依赖全部 19 张表的真实格式
    monkeypatch.setattr(build_cache, "TABLE_CONFIGS", [
        {"table": "t_company", "file_pattern": "公司库*.xlsx", "sheet": "公司",
         "pk": "代码", "header_depth": 2},
        {"table": "t_fund", "file_pattern": "基金库*.xlsx", "sheet": "基金",
         "pk": "代码", "header_depth": 2},
    ])
    return build_cache


def _xlsx(path: Path, sheet: str, rows: list[list]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["基本信息", "基本信息"])
    ws.append(["代码", "名称"])
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _rows(db: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f'SELECT * FROM "{table}"').fetchall()
    finally:
        conn.close()


def test_publish_rebuilds_only_matching_tables(bc, tmp_path: Path):
    up = tmp_path / "up"
    up.mkdir()
    bc.publish(_xlsx(up / "公司库 - 2026Q3.xlsx", "公司", [["A", "甲"]]))
    bc.publish(_xlsx(up / "基金库 - 2026Q3.xlsx", "基金", [["F1", "一号"], ["F2", "二号"]]))
    assert len(_rows(bc._DB_PATH, "t_fund")) == 2

    # 新一期公司库：只替换 t_company，t_fund 原样保留
    result = bc.publish(_xlsx(up / "公司库 - 2026Q4.xlsx", "公司", [["A", "甲"], ["B", "乙"]]))
    assert [t["table"] for t in result["tables"]] == ["t_company"]
    assert result["replaced"] == ["公司库 - 2026Q3.xlsx"]
    assert len(_rows(bc._DB_PATH, "t_company")) == 2
    assert len(_rows(bc._DB_PATH, "t_fund")) == 2
    files = sorted(p.name for p in bc.data_dir().iterdir())
    assert files == ["公司库 - 2026Q4.xlsx", "基金库 - 2026Q3.xlsx"]

    state = bc.describe()
    company = next(d for d in state["datasets"] if d["pattern"] == "公司库*.xlsx")
    assert company["file"] == "公司库 - 2026Q4.xlsx"
    assert company["tables"][0]["rows"] == 2
    assert company["tables"][0]["source_file"] == "公司库 - 2026Q4.xlsx"


def test_publish_rejects_unknown_name_and_missing_sheet(bc, tmp_path: Path):
    up = tmp_path / "up"
    up.mkdir()
    bc.publish(_xlsx(up / "公司库 - v1.xlsx", "公司", [["A", "甲"]]))
    before = bc._DB_PATH.read_bytes()

    with pytest.raises(bc.PublishError, match="不属于任何已知数据集"):
        bc.publish(_xlsx(up / "随便.xlsx", "公司", [["A", "甲"]]))
    with pytest.raises(bc.PublishError, match="缺少 sheet「公司」"):
        bc.publish(_xlsx(up / "公司库 - v2.xlsx", "别的", [["A", "甲"]]))
    with pytest.raises(bc.PublishError, match="没有数据行"):
        bc.publish(_xlsx(up / "公司库 - v3.xlsx", "公司", []))

    # 线上库与数据目录都没动，也没有残留暂存文件
    assert bc._DB_PATH.read_bytes() == before
    assert sorted(p.name for p in bc.data_dir().iterdir()) == ["公司库 - v1.xlsx"]
    assert not list(bc._DB_PATH.parent.glob("*.tmp"))


def test_query_side_does_not_rebuild_existing_db(bc, tmp_path: Path):
    up = tmp_path / "up"
    up.mkdir()
    bc.publish(_xlsx(up / "公司库 - v1.xlsx", "公司", [["A", "甲"]]))
    mtime = bc._DB_PATH.stat().st_mtime_ns
    # 源文件变新也不会在查询时现场重建（旧行为会，且会删库重建）
    (bc.data_dir() / "公司库 - v1.xlsx").touch()
    bc.ensure_cache_fresh()
    assert bc._DB_PATH.stat().st_mtime_ns == mtime


def test_full_build_is_atomic(bc, tmp_path: Path):
    data = bc.data_dir()
    data.mkdir(parents=True)
    _xlsx(data / "公司库 - v1.xlsx", "公司", [["A", "甲"]])
    _xlsx(data / "基金库 - v1.xlsx", "基金", [["F1", "一号"]])
    bc.build(force=True, verbose=False)
    assert len(_rows(bc._DB_PATH, "t_company")) == 1
    assert len(_rows(bc._DB_PATH, "t_fund")) == 1
    assert not list(bc._DB_PATH.parent.glob("*.tmp"))
