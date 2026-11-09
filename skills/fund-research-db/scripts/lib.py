"""供会话脚本 ``import`` 的查询辅助。

会话脚本要拿全量数据做二次加工（跨表聚合、排名分档、导出 Excel）时，
以前只能自己写一坨胶水：靠 ``CSAGENT_SKILLS_ROOTS`` 猜 skill 根、subprocess 拉起
``query.py``、再手写分页循环。同一段样板在多轮会话里被重复发明，而且每次的
兜底质量都不一样（有一次漏了 ``skill_root is None`` 判断，差点直接 TypeError）。

本模块把这些收进来。SKILL.md 的 ``python-path`` 让 run_script 把本目录放进 PYTHONPATH，
会话脚本直接 import 即可：

    from lib import query_df, rank_of, show

    df = query_df({
        "entity": "active_equity_funds",
        "filters": [{"field": "利得三级分类", "op": "eq", "value": "科技TMT"}],
        "select": ["基金代码", "基金简称", "综合评分"],
    })
    show(df.sort_values("综合评分", ascending=False), limit=10)

不走 subprocess，直接复用 query.py 的校验链，因此：
- 字段名/操作符非法时抛 ``QueryError``，消息就是校验器给的原文（含允许值列表）
- 不需要解析 stdout，也不会被编码问题坑到
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
# 用 __file__ 自定位，不依赖任何环境变量或当前工作目录
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from backend import KNOWN_TABLES, SqliteBackend  # noqa: E402
from build_cache import _DB_PATH, ensure_cache_fresh  # noqa: E402

# 校验链（数值列开放、列名纠错、entity/category 解析）全在 query.py，这里直接复用
from query import _CATEGORY_ALIASES, _run_list, _run_values  # noqa: E402


# 单页上限，与 entities.yaml 的 max_rows 对齐；传更大的 limit 会被压回这个值
PAGE_SIZE = 200


class QueryError(RuntimeError):
    """查询被校验层拒绝（字段名/操作符非法、列不存在等）。消息即校验器原文。"""


def skill_root() -> Path:
    """本 Skill 的根目录（自定位，不读环境变量）。"""
    return _SCRIPT_DIR.parent


def db_path() -> Path:
    """SQLite 缓存文件路径。"""
    return Path(_DB_PATH)


def resolve_table(category: str) -> str:
    """把「权益」这类简写解析成真实表名；已是表名则原样返回。"""
    raw = (category or "").strip()
    if raw in KNOWN_TABLES:
        return raw
    table = _CATEGORY_ALIASES.get(raw)
    if table is None:
        raise QueryError(
            f"未知 category: {category!r}；可用：{sorted(_CATEGORY_ALIASES)} 或 {sorted(KNOWN_TABLES)}"
        )
    return table


def _backend() -> SqliteBackend:
    ensure_cache_fresh()
    return SqliteBackend(_DB_PATH)


def _parse(text: str) -> dict[str, Any]:
    """query.py 内部函数的返回：成功是结果 JSON；失败是 {"ok": false, "error": {...}}、
    backend 的 {"error": "..."} 或校验器的纯文本。"""
    stripped = (text or "").strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        raise QueryError(stripped) from None
    if isinstance(data, dict) and (data.get("ok") is False or data.get("error")):
        err = data.get("error")
        raise QueryError(str(err.get("message") or err) if isinstance(err, dict) else str(err))
    return data


def query(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次 list 查询，返回原始 payload（含 matched / rows / next_offset）。

    注意 ``matched`` 是**满足 filters 的总数**，不是全表行数——它大于 ``len(rows)``
    只说明还有下一页，不代表筛选没生效。
    """
    return _parse(asyncio.run(_run_list(dict(payload), _backend())))


def query_all(
    payload: dict[str, Any],
    *,
    page_size: int = PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """自动翻页取满，返回合并后的 rows。

    Args:
        payload: 同 ``query``，其中 limit/offset 由本函数接管。
        page_size: 每页条数，超过 max_rows 会被服务端压回 200。
        max_rows: 取到这么多条就停；None 表示取全。
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = query({**payload, "limit": page_size, "offset": offset})
        batch = page.get("rows") or []
        rows.extend(batch)
        if max_rows is not None and len(rows) >= max_rows:
            return rows[:max_rows]
        nxt = page.get("next_offset")
        if not page.get("truncated") or not nxt or not batch:
            return rows
        offset = nxt


def get_fund(category: str, keyword: str) -> dict[str, Any]:
    """单只基金完整档案（含「一句话基金画像」），**直接返回字段字典**。

    与 query_all / fields 保持一致：拿到就能 ``f["综合评分"]``，不用再剥一层信封。
    （早先返回的是 ``{"entity","match","fund"}`` 外壳，调用方按直觉写 ``f.get("基金代码")``
    会全拿到 None 且不报错，白白浪费排查轮次。）

    名称写法和库里简称不一致时会做近似匹配（「工银瑞信双利债券」→「工银双利A」），
    要知道是不是近似对上的，用 get_fund_raw 看 match 是否为 fuzzy_name。
    未命中抛 QueryError；关键字命中多只时，异常消息里带候选列表；
    都没对上但有名字相近的，异常消息里带 similar（只供核对，不是匹配结果）。
    """
    data = _parse(_backend().get_fund(resolve_table(category), keyword))
    fund = data.get("fund")
    if isinstance(fund, dict) and fund:
        return fund
    match = data.get("match")
    if match == "ambiguous":
        cands = data.get("candidates") or data.get("rows") or []
        raise QueryError(f"关键字 {keyword!r} 命中多只，请用基金代码精确查询；候选：{cands}")
    similar = data.get("similar")
    if similar:
        raise QueryError(f"未找到基金：category={category!r} keyword={keyword!r}；名字相近的有：{similar}")
    raise QueryError(f"未找到基金：category={category!r} keyword={keyword!r}")


def get_fund_raw(category: str, keyword: str) -> dict[str, Any]:
    """原始信封（含 entity / match / fund），需要判断匹配方式时才用。"""
    return _parse(_backend().get_fund(resolve_table(category), keyword))


def fields(category: str) -> list[str]:
    """某张表当前的真实列名清单——猜不准列名时先调它。"""
    data = _parse(_backend().fields(resolve_table(category)))
    return list(data.get("columns") or [])


def values(
    category: str,
    field: str,
    filters: list[dict[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """某个字段有哪些取值：文本列给取值与条数，数值列给分布。

    想知道分类名怎么写、某列大致什么范围时用它，不用自己拉全表再分组统计。
    """
    payload = {"entity": category, "field": field, "filters": filters or [], "limit": limit}
    return _parse(_run_values(payload, _backend()))


def query_df(payload: dict[str, Any], *, max_rows: int | None = None):
    """同 ``query_all``，返回 ``pandas.DataFrame``；给了 select 时列顺序与 select 一致（空结果也有列）。"""
    import pandas as pd

    rows = query_all(payload, max_rows=max_rows)
    return pd.DataFrame(rows, columns=payload.get("select") or None)


def rank_of(
    df,
    key_col: str,
    key: Any,
    value_cols: list[str],
    *,
    ascending: bool = False,
) -> list[dict[str, Any]]:
    """某个对象在 ``df`` 里各指标的名次，「排名 22/168（前 13.1%）」这类同类比较一步出结果。

    默认数值越大名次越靠前（``ascending=True`` 反过来，比如回撤、费率）；并列取最好名次；
    空值不参与排名，分母是该指标有值的条数。

    Args:
        df: 同类全量，通常来自 ``query_df``
        key_col: 定位对象的列，如 ``基金代码``
        key: 对象在该列的值，如 ``485111.OF``
        value_cols: 要看名次的指标列
    """
    import pandas as pd

    hit = df.index[df[key_col] == key]
    if len(hit) == 0:
        raise QueryError(f"{key_col}={key!r} 不在数据里，先确认同类范围的筛选条件包含它")
    out: list[dict[str, Any]] = []
    for col in value_cols:
        series = pd.to_numeric(df[col], errors="coerce")
        valid = series.dropna()
        value = series[hit[0]]
        if pd.isna(value):
            out.append({"指标": col, "值": None, "名次": None, "有效数": len(valid), "前百分比": None})
            continue
        better = (valid < value).sum() if ascending else (valid > value).sum()
        rank = int(better) + 1
        out.append({
            "指标": col,
            "值": float(value),
            "名次": rank,
            "有效数": len(valid),
            "前百分比": round(rank / len(valid) * 100, 1),
        })
    return out


def show(data, columns: list[str] | None = None, *, limit: int = 30, digits: int = 2) -> None:
    """把结果按 Markdown 表格打印前 ``limit`` 行。

    只打需要的列、数值保留 ``digits`` 位：整行 dict / JSON 往外 print 很快就超过工具输出上限，
    被外置后还得再调 read_artifact 分页读（见 2026-10-22 审计）。

    Args:
        data: DataFrame，或 ``query_all`` / ``rank_of`` 返回的 dict 列表
        columns: 要打印的列，不传打印全部
    """
    import math

    import pandas as pd

    df = data if isinstance(data, pd.DataFrame) else pd.DataFrame(list(data))
    if columns:
        df = df[list(columns)]
    total = len(df)
    df = df.head(limit)

    def cell(v: Any) -> str:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return ""
        if isinstance(v, float):
            return f"{v:.{digits}f}"
        return str(v).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(str(c) for c in df.columns) + " |",
        "|" + "---|" * len(df.columns),
        *("| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)),
    ]
    if total > limit:
        lines.append(f"（共 {total} 行，只显示前 {limit} 行）")
    print("\n".join(lines))
