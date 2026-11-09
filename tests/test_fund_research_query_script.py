"""skills/fund-research-db/scripts/query.py 单测。

脚本不在 src/csagent 包内：把脚本目录临时放到 sys.path 最前面，按生产里 `python query.py`
子进程的同目录 import 方式加载。fund-product-query 也有 query.py，所以先摘掉 sys.modules
里的同名模块、用完再清掉，免得两个 skill 的模块互相串。
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "fund-research-db" / "scripts"
_MODULES = ("query", "lib", "backend", "build_cache")
_TABLE = "industry_rotation_equity"


@pytest.fixture()
def rq(monkeypatch):
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    for name in _MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import query

    yield query
    for name in _MODULES:
        sys.modules.pop(name, None)


@pytest.fixture()
def backend(rq, tmp_path):
    """建一张小表：「近三年最大回撤(%)」是 entities.yaml 没声明的数值列，「近三年换手率风格」是没开放的文本列。"""
    db = tmp_path / "fund_research.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        f'CREATE TABLE "{_TABLE}" ("基金代码" TEXT, "基金简称" TEXT, "利得三级分类" TEXT, '
        '"近三年换手率风格" TEXT, "基金规模(亿元)" REAL, "近三年行业数" REAL, "近三年最大回撤(%)" REAL)'
    )
    conn.executemany(
        f'INSERT INTO "{_TABLE}" VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
            ("000001.OF", "甲成长", "科技TMT", "高", 12.0, 7.0, -30.5),
            ("000002.OF", "乙价值", "均衡配置", "低", 3.5, 5.0, -12.0),
            ("000003.OF", "丙科技", "科技TMT", "中", 40.0, 9.0, -45.0),
            ("000004.OF", "丁红利", None, "低", 8.0, 5.0, -8.25),
        ],
    )
    conn.commit()
    conn.close()
    return rq.SqliteBackend(db)


def _list(rq, backend, payload):
    return json.loads(asyncio.run(rq._run_list(payload, backend)))


def _codes(data):
    return [row["基金代码"] for row in data["rows"]]


# ------------------------------------------------------------------
# list：数值列开放、ne、列名纠错
# ------------------------------------------------------------------


def test_undeclared_numeric_column_can_filter_and_sort(rq, backend):
    data = _list(rq, backend, {
        "entity": _TABLE,
        "filters": [{"field": "近三年最大回撤(%)", "op": "gt", "value": -40}],
        "order_by": {"field": "近三年最大回撤(%)", "direction": "asc"},
        "select": ["基金代码"],
    })
    assert _codes(data) == ["000001.OF", "000002.OF", "000004.OF"]


def test_declared_numeric_field_gets_all_numeric_ops(rq, backend):
    # entities.yaml 里「基金规模(亿元)」只写了 gt/gte/lt/lte
    data = _list(rq, backend, {
        "entity": _TABLE,
        "filters": [{"field": "基金规模(亿元)", "op": "eq", "value": 3.5}],
        "select": ["基金代码"],
    })
    assert _codes(data) == ["000002.OF"]


def test_ne_keeps_null_rows(rq, backend):
    data = _list(rq, backend, {
        "entity": _TABLE,
        "filters": [{"field": "利得三级分类", "op": "ne", "value": "科技TMT"}],
        "select": ["基金代码"],
    })
    assert _codes(data) == ["000002.OF", "000004.OF"]


def test_category_alias_works_for_list(rq, backend):
    data = _list(rq, backend, {"category": "权益轮动", "select": ["基金代码"]})
    assert data["entity"] == _TABLE
    assert data["matched"] == 4


def test_wrong_sort_field_suggests_closest_column(rq, backend):
    data = _list(rq, backend, {"entity": _TABLE, "order_by": {"field": "基金规模(合计)(亿元)", "direction": "desc"}})
    assert data["ok"] is False
    assert data["error"]["code"] == "FIELD_NOT_FOUND"
    assert data["error"]["suggestions"][0] == "基金规模(亿元)"


def test_wrong_select_column_suggests_closest(rq, backend):
    data = _list(rq, backend, {"entity": _TABLE, "select": ["基金代码", "基金名称"]})
    assert data["error"]["suggestions"]["基金名称"][0] == "基金简称"


def test_closed_text_column_says_why(rq, backend):
    data = _list(rq, backend, {
        "entity": _TABLE,
        "filters": [{"field": "近三年换手率风格", "op": "eq", "value": "高"}],
    })
    assert data["error"]["code"] == "FIELD_NOT_FILTERABLE"
    assert "文本列" in data["error"]["message"]
    assert "利得三级分类" in data["error"]["message"]


# ------------------------------------------------------------------
# fields / get：entity 和 category 都认
# ------------------------------------------------------------------


def test_fields_accepts_entity_and_marks_text_columns(rq, backend):
    data = json.loads(rq._run_fields({"entity": _TABLE}, backend))
    assert data["columns"][0] == "基金代码"
    assert data["text_columns"] == ["基金代码", "基金简称", "利得三级分类", "近三年换手率风格"]
    assert "近三年换手率风格" not in data["text_filterable"]
    assert "ne" in data["text_filterable"]["利得三级分类"]


def test_get_accepts_entity(rq, backend):
    data = json.loads(rq._run_get({"entity": _TABLE, "keyword": "000002"}, backend))
    assert data["match"] == "exact_code"
    assert data["fund"]["基金简称"] == "乙价值"


def test_unknown_entity_is_reported(rq, backend):
    data = json.loads(rq._run_fields({"entity": "industry_rotation"}, backend))
    assert data["error"]["code"] == "UNKNOWN_ENTITY"
    assert data["error"]["suggestions"][0].startswith("industry_rotation")


# ------------------------------------------------------------------
# get：名称写法和库里简称不一致时的近似匹配
# ------------------------------------------------------------------

_FIP = "active_fixed_income_plus_funds"


@pytest.fixture()
def named(rq, tmp_path):
    """库里简称的几种写法：公司两字简称（工银）、非前缀简称（大摩）、写全公司名（工银瑞信稳润）、份额。"""
    db = tmp_path / "named.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(f'CREATE TABLE "{_FIP}" ("基金代码" TEXT, "基金简称" TEXT, "基金公司" TEXT, "一句话基金画像" TEXT)')
    conn.executemany(
        f'INSERT INTO "{_FIP}" VALUES (?, ?, ?, ?)',
        [
            ("485111.OF", "工银双利A", "工银瑞信基金", "二级债基"),
            ("006004.OF", "工银添祥一年定开", "工银瑞信基金", "定开"),
            ("001000.OF", "工银瑞信稳润一年持有A", "工银瑞信基金", "持有期"),
            ("481012.OF", "工银深证红利ETF联接A", "工银瑞信基金", "联接"),
            ("481013.OF", "工银深证红利ETF", "工银瑞信基金", "ETF"),
            ("000024.OF", "大摩双利增强A", "摩根士丹利基金", "增强"),
            ("001001.OF", "华夏双债增强A", "华夏基金", "增强"),
            ("002001.OF", "鹏扬双利A", "鹏扬基金", "A 份额"),
            ("002002.OF", "鹏扬双利C", "鹏扬基金", "C 份额"),
        ],
    )
    conn.commit()
    conn.close()
    return rq.SqliteBackend(db)


def _get(rq, backend, keyword):
    return json.loads(rq._run_get({"entity": _FIP, "keyword": keyword}, backend))


@pytest.mark.parametrize(
    ("keyword", "code"),
    [
        ("工银瑞信双利债券", "485111.OF"),  # 2026-10-22 审计原句：公司全称 + 类型词
        ("工银瑞信双利债券型证券投资基金", "485111.OF"),
        ("工银双利债券", "485111.OF"),
        ("摩根士丹利双利增强", "000024.OF"),  # 库里简称「大摩」不是公司名的前缀
        ("工银瑞信添祥一年定期开放债券型", "006004.OF"),  # 库里把「定期开放」简成了「定开」
        ("工银瑞信稳润一年持有期", "001000.OF"),  # 库里简称写全了公司名
        ("工银瑞信深证红利ETF联接", "481012.OF"),  # 主体一字不差的只有联接，不再列 ETF 本身
    ],
)
def test_get_matches_names_written_differently(rq, named, keyword, code):
    data = _get(rq, named, keyword)
    assert data["match"] == "fuzzy_name"
    assert data["fund"]["基金代码"] == code
    assert data["matched_name"] in data["note"]


def test_get_fuzzy_prefers_the_share_class_asked(rq, named):
    data = _get(rq, named, "鹏扬双利债券C")
    assert data["fund"]["基金代码"] == "002002.OF"
    assert "份额" not in data["note"]


def test_get_fuzzy_without_share_class_lists_all_classes(rq, named):
    data = _get(rq, named, "鹏扬双利债券")
    assert data["match"] == "ambiguous"
    assert data["fuzzy"] is True
    assert {c["基金代码"] for c in data["candidates"]} == {"002001.OF", "002002.OF"}


def test_get_fuzzy_notes_share_class_not_in_db(rq, named):
    data = _get(rq, named, "工银瑞信双利债券C")
    assert data["fund"]["基金代码"] == "485111.OF"
    assert "C 类" in data["note"]


def test_get_miss_offers_similar_names_only_for_checking(rq, named):
    data = _get(rq, named, "华夏双利增强")
    assert data["match"] == "none"
    assert [c["基金简称"] for c in data["similar"]] == ["华夏双债增强A"]
    assert "只供核对" in data["hint"]


@pytest.mark.parametrize("keyword", ["485112", "工银瑞信基金", "双利"])
def test_get_no_fuzzy_guess_for_codes_company_names_or_short_words(rq, named, keyword):
    data = _get(rq, named, keyword)
    assert data["match"] in ("none", "ambiguous")
    assert "fund" not in data
    assert "similar" not in data


def test_name_core_keeps_etf_and_strips_share_class(rq):
    import backend

    assert backend._name_core("工银深证红利ETF") == ("工银深证红利ETF", "")
    assert backend._name_core("工银双利A类") == ("工银双利", "A")
    assert backend._name_core("华夏磐晟灵活（LOF）C") == ("华夏磐晟灵活", "C")


def test_lib_get_fund_follows_fuzzy_matches(rq, named, monkeypatch):
    import lib

    monkeypatch.setattr(lib, "_backend", lambda: named)
    assert lib.get_fund(_FIP, "工银瑞信双利债券")["基金代码"] == "485111.OF"
    with pytest.raises(lib.QueryError, match="华夏双债增强A"):
        lib.get_fund(_FIP, "华夏双利增强")


# ------------------------------------------------------------------
# values
# ------------------------------------------------------------------


def test_values_text_counts_and_nulls(rq, backend):
    data = json.loads(rq._run_values({"entity": _TABLE, "field": "利得三级分类"}, backend))
    assert data["type"] == "text"
    assert data["values"] == [{"value": "科技TMT", "count": 2}, {"value": "均衡配置", "count": 1}]
    assert (data["nulls"], data["distinct"], data["truncated"]) == (1, 2, False)


def test_values_limit_truncates(rq, backend):
    data = json.loads(rq._run_values({"entity": _TABLE, "field": "利得三级分类", "limit": 1}, backend))
    assert data["returned"] == 1
    assert data["truncated"] is True


def test_values_numeric_distribution_with_filters(rq, backend):
    data = json.loads(rq._run_values({
        "entity": _TABLE,
        "field": "近三年行业数",
        "filters": [{"field": "利得三级分类", "op": "eq", "value": "科技TMT"}],
    }, backend))
    assert data["type"] == "number"
    assert (data["min"], data["median"], data["max"]) == (7.0, 8.0, 9.0)
    assert data["values"] == [{"value": 7.0, "count": 1}, {"value": 9.0, "count": 1}]


def test_values_unknown_field_suggests_closest(rq, backend):
    data = json.loads(rq._run_values({"entity": _TABLE, "field": "近三年回撤(%)"}, backend))
    assert data["ok"] is False
    assert data["error"]["suggestions"][0] == "近三年最大回撤(%)"


# ------------------------------------------------------------------
# 命令行出口：统一信封、stdin 入参
# ------------------------------------------------------------------


@pytest.fixture()
def cli(rq, backend, monkeypatch, capsys):
    """按 tools.yaml 的方式跑 main()：返回 (exit code, 解析后的 stdout)。"""
    monkeypatch.setattr(rq, "ensure_cache_fresh", lambda: None)
    monkeypatch.setattr(rq, "_DB_PATH", backend._db_path)

    def run(action, payload, *, stdin=False):
        text = json.dumps(payload, ensure_ascii=False)
        if stdin:
            monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        monkeypatch.setattr(sys, "argv", ["query.py", "--action", action, "--payload", "-" if stdin else text])
        code = rq.main()
        return code, json.loads(capsys.readouterr().out)

    return run


def test_cli_success_is_wrapped_with_ok(cli):
    code, out = cli("values", {"entity": _TABLE, "field": "近三年换手率风格"})
    assert code == 0
    assert out["ok"] is True
    assert out["values"][0] == {"value": "低", "count": 2}


def test_cli_reads_payload_from_stdin(cli):
    code, out = cli("get", {"entity": _TABLE, "keyword": "000003"}, stdin=True)
    assert code == 0
    assert out["fund"]["基金简称"] == "丙科技"


def test_cli_turns_plain_text_rejections_into_the_envelope(cli):
    # 缺 op：先过了列名预检，由通用校验器以纯文本拒绝
    code, out = cli("list", {"entity": _TABLE, "filters": [{"field": "利得三级分类", "value": "x"}]})
    assert code == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "QUERY_REJECTED"
    assert "QuerySpec" in out["error"]["message"]


def test_cli_bad_payload(cli, monkeypatch, capsys, rq):
    monkeypatch.setattr(sys, "argv", ["query.py", "--action", "get", "--payload", "{坏的"])
    assert rq.main() == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "BAD_PAYLOAD"


# ------------------------------------------------------------------
# lib.py 走同一条校验链
# ------------------------------------------------------------------


def test_lib_shares_the_same_rules(rq, backend, monkeypatch):
    import lib

    monkeypatch.setattr(lib, "_backend", lambda: backend)
    rows = lib.query_all({
        "category": "权益轮动",
        "filters": [{"field": "近三年最大回撤(%)", "op": "lt", "value": -40}],
        "select": ["基金代码"],
    })
    assert rows == [{"基金代码": "000003.OF"}]
    assert lib.values(_TABLE, "利得三级分类")["values"][0] == {"value": "科技TMT", "count": 2}
    with pytest.raises(lib.QueryError, match="最接近"):
        lib.query({"entity": _TABLE, "select": ["基金名称"]})


def test_lib_dataframe_helpers(rq, backend, monkeypatch, capsys):
    import lib

    monkeypatch.setattr(lib, "_backend", lambda: backend)
    df = lib.query_df({"entity": _TABLE, "select": ["基金代码", "近三年最大回撤(%)", "基金规模(亿元)"]})
    assert list(df.columns) == ["基金代码", "近三年最大回撤(%)", "基金规模(亿元)"]

    ranks = {r["指标"]: r for r in lib.rank_of(df, "基金代码", "000002.OF", ["基金规模(亿元)", "近三年最大回撤(%)"])}
    assert (ranks["基金规模(亿元)"]["名次"], ranks["基金规模(亿元)"]["有效数"]) == (4, 4)  # 3.5 最小
    assert ranks["近三年最大回撤(%)"]["名次"] == 2  # -12.0 只输给 -8.25
    with pytest.raises(lib.QueryError):
        lib.rank_of(df, "基金代码", "999999.OF", ["基金规模(亿元)"])

    lib.show(df, ["基金代码", "基金规模(亿元)"], limit=2)
    out = capsys.readouterr().out
    assert "| 000001.OF | 12.00 |" in out
    assert "共 4 行，只显示前 2 行" in out
