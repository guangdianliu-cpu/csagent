"""利得基金研究数据库 · 统一查询入口。

用法：
    python query.py --action <list|get|fields|values> --payload '<JSON>'   # --payload - 从 stdin 读

模型侧不直接跑本脚本：tools.yaml 把四个 action 声明成原生工具 fund_db_get / fund_db_list /
fund_db_fields / fund_db_values，内核把工具参数 JSON 经 stdin 交给 ``--payload -``。

输出统一信封：成功 {"ok": true, ...}，失败 {"ok": false, "error": {"code", "message", "suggestions"?}}，
失败时 exit code 非 0。

四个 action，payload 里用 entity（表名）或 category（中文别名）指定表，两个键都认：
  list   结构化筛选/排序/选列，走通用 SemanticQueryExecutor + Catalog 校验（entities.yaml）
         payload: {"entity": "...", "filters": [...], "order_by": {...}, "limit": 20, "select": [...]}
         数值列（建表时类型为 REAL）不受 entities.yaml 白名单限制，都能筛选和排序
  get    单只基金完整档案（含"一句话基金画像"），按基金代码精确或基金简称模糊匹配
         payload: {"category": "纯债|权益|固收+", "keyword": "000001.OF 或 名称关键字"}
  fields 某个库当前真实列清单（自查工具，避免猜列名），并标出文本列和能筛选的文本字段
         payload: {"category": "纯债|权益|固收+"}
  values 某个字段有哪些取值：文本列给取值与条数，数值列给分布；可带 filters 先筛
         payload: {"entity": "...", "field": "利得三级分类", "filters": [...], "limit": 50}

entity/table 全名：active_bond_funds / active_equity_funds / active_fixed_income_plus_funds
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
# 防御 csagent 未正确安装/指向陈旧路径的情况（同名包被别的 editable install 抢注时，
# 显式把本仓库 src/ 排到最前，保证总是导入"当前项目"的 csagent，而不是环境里的旧副本）
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from backend import KNOWN_TABLES, SqliteBackend  # noqa: E402
from build_cache import _DB_PATH, ensure_cache_fresh  # noqa: E402

from csagent.react.models import CustomerScope  # noqa: E402
from csagent.tools.semantic.catalog import EntityDef, FieldDef, QueryCatalog  # noqa: E402
from csagent.tools.semantic.executor import SemanticQueryExecutor  # noqa: E402
from csagent.tools.semantic.models import FilterSpec, QuerySpec  # noqa: E402

_ENTITIES_YAML = Path(__file__).resolve().parent / "entities.yaml"

# 数值列一律开放筛选和排序，不再逐列写进 entities.yaml：白名单只覆盖了少数核心列，
# 其余数值列被拒后模型只能写脚本绕（见 2026-10-22 审计，一轮里为此写了 3 个探查脚本）
_NUMERIC_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in"})
_VALUES_LIMIT = 50
_VALUES_MAX_LIMIT = 200
_UNIT = re.compile(r"[（(][^（）()]*[）)]")

_CATEGORY_ALIASES = {
    # 纯债
    "纯债": "active_bond_funds",
    "主动纯债": "active_bond_funds",
    "纯债基金": "active_bond_funds",
    # 权益
    "权益": "active_equity_funds",
    "主动权益": "active_equity_funds",
    "权益基金": "active_equity_funds",
    # 固收+
    "固收+": "active_fixed_income_plus_funds",
    "固收＋": "active_fixed_income_plus_funds",
    "主动固收+": "active_fixed_income_plus_funds",
    "固收加": "active_fixed_income_plus_funds",
    # QDII
    "QDII": "qdii_equity_funds",
    "QDII权益": "qdii_equity_funds",
    "QDII另类": "qdii_equity_funds",
    "QDII债券": "qdii_bond_funds",
    # 基金经理
    "基金经理": "fund_managers_equity",
    "权益基金经理": "fund_managers_equity",
    "主动权益基金经理": "fund_managers_equity",
    "固收+基金经理": "fund_managers_fixed_income",
    "主动固收+基金经理": "fund_managers_fixed_income",
    # 交易能力
    "交易能力": "fund_trading_ability",
    "换手率": "fund_trading_ability",
    # 基金公司
    "基金公司": "fund_companies",
    "基金管理人": "fund_companies",
    "管理人": "fund_companies",
    # 可转债
    "转债": "convertible_bond_funds",
    "可转债": "convertible_bond_funds",
    "可转债基金": "convertible_bond_funds",
    # 量化策略
    "量化": "quant_equity_funds",
    "量化策略": "quant_equity_funds",
    "量化指增": "quant_equity_funds",
    "量化选股": "quant_equity_funds",
    # 业绩归因
    "业绩归因": "attribution_equity",
    "归因": "attribution_equity",
    "权益归因": "attribution_equity",
    "主动权益归因": "attribution_equity",
    "固收+归因": "attribution_fixed_income",
    "固收+业绩归因": "attribution_fixed_income",
    # 行业轮动
    "行业轮动": "industry_rotation_equity",
    "轮动": "industry_rotation_equity",
    "权益轮动": "industry_rotation_equity",
    "固收+轮动": "industry_rotation_fixed_income",
    # 行业配置工具
    "行业配置": "industry_allocation_sw",
    "行业配置工具": "industry_allocation_sw",
    "申万配置": "industry_allocation_sw",
    "申万行业配置": "industry_allocation_sw",
    "中信配置": "industry_allocation_zx",
    "中信行业配置": "industry_allocation_zx",
    # 行业投资能力
    "行业投资能力": "manager_industry_capabilities",
    "主题投资能力": "manager_industry_capabilities",
    # 私募基金
    "私募": "private_funds",
    "私募基金": "private_funds",
    "私募产品": "private_funds",
}


def _resolve_table(category: str) -> str | None:
    if category in KNOWN_TABLES:
        return category
    return _CATEGORY_ALIASES.get(category.strip())


def _resolve_entity(payload: dict) -> tuple[str | None, str]:
    """entity 和 category 两个键都认，表名和中文别名都行。

    模型在返回里看到的是 entity 名，拿它去调 fields/get 很自然，不该因此报「未知 category」。
    返回 (表名或 None, 原始输入)。
    """
    given = [str(payload.get(key) or "").strip() for key in ("entity", "category")]
    given = [raw for raw in given if raw]
    for raw in given:
        table = _resolve_table(raw)
        if table is not None:
            return table, raw
    return None, given[0] if given else ""


def _fail(code: str, message: str, **extra: Any) -> str:
    """失败统一成 {"ok": false, "error": {"code", "message", ...}}；message 本身写清该怎么改。"""
    return json.dumps({"ok": False, "error": {"code": code, "message": message, **extra}}, ensure_ascii=False)


def _unknown_entity(raw: str) -> str:
    tables = sorted(KNOWN_TABLES)
    near: list[str] = []
    for hit in difflib.get_close_matches(raw, [*tables, *_CATEGORY_ALIASES], n=5, cutoff=0.3):
        table = _resolve_table(hit)
        if table and table not in near:
            near.append(table)
    return _fail("UNKNOWN_ENTITY", f"未知 entity/category: {raw!r}", suggestions=near[:3], allowed=tables)


def _closest(name: str, columns: list[str], n: int = 3) -> list[str]:
    """给写错的字段名找最接近的真实列名。

    先看去掉括号单位后的包含关系（「基金规模(亿元)」↔「基金规模(合计)(亿元)」），再按字形相似度补齐。
    """
    core = _UNIT.sub("", name).strip() or name
    contained = sorted(
        (c for c in columns if core in c),
        key=lambda c: difflib.SequenceMatcher(None, name, c).ratio(),
        reverse=True,
    )
    similar = difflib.get_close_matches(name, columns, n=n, cutoff=0.4)
    picked: list[str] = []
    for col in (*contained, *similar):
        if col not in picked:
            picked.append(col)
    return picked[:n]


def _missing(role: str, name: str, columns: list[str]) -> str:
    near = _closest(name, columns)
    hint = f"，最接近的：{'、'.join(near)}" if near else "；先用 fund_db_fields 查这张表的真实列名"
    return _fail("FIELD_NOT_FOUND", f"{role}字段 '{name}' 不存在{hint}", suggestions=near)


def _open_catalog(table: str, column_types: dict[str, str]) -> QueryCatalog:
    """加载 entities.yaml，把该表的数值列全部开放筛选排序，并给支持 eq 的字段补上 ne。"""
    catalog = QueryCatalog.load(_ENTITIES_YAML)
    entity = catalog.get(table)
    if entity is None:
        return catalog
    fields = dict(entity.fields)
    for name, fdef in fields.items():
        if "eq" in fdef.ops and "ne" not in fdef.ops:
            fields[name] = replace(fdef, ops=fdef.ops | {"ne"})
    for name, col_type in column_types.items():
        if col_type == "REAL":
            fdef = fields.get(name) or FieldDef(name=name, type="number")
            fields[name] = replace(fdef, filterable=True, sortable=True, ops=fdef.ops | _NUMERIC_OPS)
    entities = catalog.all_entities()
    entities[table] = replace(entity, fields=fields)
    return QueryCatalog(entities)


def _text_filterable(entity: EntityDef | None, column_types: dict[str, str]) -> dict[str, list[str]]:
    """本表能筛选的文本字段及各自允许的操作符。"""
    if entity is None:
        return {}
    return {
        name: sorted(fdef.ops)
        for name, fdef in entity.fields.items()
        if fdef.filterable and name in column_types and column_types[name] != "REAL"
    }


def _precheck(entity: EntityDef | None, column_types: dict[str, str], payload: dict) -> str | None:
    """先对照真实列校验一遍。

    通用校验器只认 entities.yaml，分不清「列不存在」和「列存在但没开放」，也给不出正确列名，
    模型只能反复猜。这里列名写错时给出最接近的真实列，列存在但不能用时说清原因。
    """
    columns = list(column_types)
    fields = entity.fields if entity else {}
    for flt in payload.get("filters") or []:
        if not isinstance(flt, dict):
            continue  # 形状不对交给通用校验器报
        name = str(flt.get("field") or "")
        if name not in column_types:
            return _missing("筛选", name, columns)
        fdef = fields.get(name)
        if fdef is None or not fdef.filterable:
            allowed = "、".join(_text_filterable(entity, column_types)) or "无"
            return _fail(
                "FIELD_NOT_FILTERABLE",
                f"筛选字段 '{name}' 是文本列，没有开放筛选。数值列都能筛选和排序；本表能筛选的文本字段：{allowed}",
            )
        op = flt.get("op")
        if isinstance(op, str) and op not in fdef.ops:
            return _fail("OP_NOT_ALLOWED", f"字段 '{name}' 不支持操作符 '{op}'，允许: {sorted(fdef.ops)}")
    order_by = payload.get("order_by")
    if isinstance(order_by, dict):
        name = str(order_by.get("field") or "")
        if name not in column_types:
            return _missing("排序", name, columns)
        fdef = fields.get(name)
        if fdef is None or not fdef.sortable:
            sortable = [
                n for n, f in fields.items() if f.sortable and n in column_types and column_types[n] != "REAL"
            ]
            return _fail(
                "FIELD_NOT_SORTABLE",
                f"排序字段 '{name}' 是文本列，不能排序。数值列都能排序；本表能排序的文本字段：{'、'.join(sortable) or '无'}",
            )
    select = payload.get("select")
    if isinstance(select, list):
        bad = [str(c) for c in select if str(c) not in column_types]
        if bad:
            near = {c: _closest(c, columns) for c in bad}
            tips = "；".join(f"'{c}' 最接近 {'、'.join(v) or '（无相近列）'}" for c, v in near.items())
            return _fail("FIELD_NOT_FOUND", f"select 里有不存在的列：{tips}", suggestions=near)
    return None


def _emit(text: str) -> int:
    """统一出口契约：stdout 永远是 JSON 信封，失败时 exit code 非 0。

    成功：``{"ok": true, ...原结果字段}``；失败：``{"ok": false, "error": {"code", "message", ...}}``。
    校验失败有的是 SemanticQueryOutcome.text 里给人看的**纯文本**，有的是 backend 的
    ``{"error": "..."}``，这里统一收成一种形状——以前纯文本也走 exit 0，调用方脚本
    ``json.loads(stdout)`` 直接抛 JSONDecodeError，真正的原因被吞掉，模型只能盲猜
    （见 audit wecom_BoWen 05:17，猜了三轮被 StallGuard 收口）。
    """
    stripped = (text or "").strip()
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        print(_fail("QUERY_REJECTED", stripped or "查询没有返回内容"))
        return 1
    if parsed.get("ok") is False or parsed.get("error"):
        err = parsed.get("error")
        if not isinstance(err, dict):
            err = {"code": "QUERY_REJECTED", "message": str(err or "查询失败")}
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **parsed}, ensure_ascii=False, default=str))
    return 0


async def _run_list(payload: dict, backend: SqliteBackend) -> str:
    table, raw = _resolve_entity(payload)
    if table is None:
        return _unknown_entity(raw)
    column_types = backend.column_types(table)
    catalog = _open_catalog(table, column_types)
    err = _precheck(catalog.get(table), column_types, payload)
    if err:
        return err
    executor = SemanticQueryExecutor(catalog=catalog, backend=backend)
    scope = CustomerScope(customer_id="", session_id="fund-research-db-cli")
    return await executor.execute({**payload, "entity": table}, scope)


def _run_get(payload: dict, backend: SqliteBackend) -> str:
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return _fail("MISSING_PARAM", "缺少参数 keyword（基金代码或名称关键字）")
    table, raw = _resolve_entity(payload)
    if table is None:
        return _unknown_entity(raw)
    return backend.get_fund(table, keyword)


def _run_fields(payload: dict, backend: SqliteBackend) -> str:
    table, raw = _resolve_entity(payload)
    if table is None:
        return _unknown_entity(raw)
    column_types = backend.column_types(table)
    entity = _open_catalog(table, column_types).get(table)
    return json.dumps(
        {
            "entity": table,
            "columns": list(column_types),
            "column_count": len(column_types),
            "text_columns": [c for c, t in column_types.items() if t != "REAL"],
            "text_filterable": _text_filterable(entity, column_types),
            "hint": "columns 里除 text_columns 以外都是数值列，都能筛选（eq/ne/gt/gte/lt/lte/in）和排序；"
                    "文本列只有 text_filterable 里的能筛选；某个字段有哪些取值用 fund_db_values 查",
        },
        ensure_ascii=False,
    )


def _run_values(payload: dict, backend: SqliteBackend) -> str:
    table, raw = _resolve_entity(payload)
    if table is None:
        return _unknown_entity(raw)
    name = str(payload.get("field") or "").strip()
    if not name:
        return _fail("MISSING_PARAM", "缺少参数 field（要看取值的字段名）")
    column_types = backend.column_types(table)
    if name not in column_types:
        return _missing("", name, list(column_types))
    filters_raw = payload.get("filters") or []
    err = _precheck(_open_catalog(table, column_types).get(table), column_types, {"filters": filters_raw})
    if err:
        return err
    try:
        filters = [FilterSpec(**item) for item in filters_raw if isinstance(item, dict)]
    except ValueError as exc:
        return _fail("BAD_PAYLOAD", f"filters 格式错误: {exc}")
    try:
        limit = int(payload.get("limit") or _VALUES_LIMIT)
    except (TypeError, ValueError):
        limit = _VALUES_LIMIT
    limit = max(1, min(limit, _VALUES_MAX_LIMIT))
    return backend.field_values(table, name, QuerySpec(entity=table, filters=filters), limit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True, choices=["list", "get", "fields", "values"])
    # Skill 原生工具（tools.yaml）传 --payload -：参数 JSON 从 stdin 读，不经命令行转义
    parser.add_argument("--payload", default="{}", help="JSON 对象；写 - 表示从 stdin 读")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.payload == "-" else args.payload
    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        print(_fail("BAD_PAYLOAD", f"payload 解析失败: {exc}"))
        return 1

    try:
        ensure_cache_fresh()
    except FileNotFoundError as exc:
        print(_fail("DATA_MISSING", f"源数据文件缺失: {exc}"))
        return 1

    backend = SqliteBackend(_DB_PATH)

    if args.action == "list":
        return _emit(asyncio.run(_run_list(payload, backend)))
    if args.action == "get":
        return _emit(_run_get(payload, backend))
    if args.action == "values":
        return _emit(_run_values(payload, backend))
    return _emit(_run_fields(payload, backend))


if __name__ == "__main__":
    raise SystemExit(main())
