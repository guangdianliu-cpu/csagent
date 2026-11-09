"""将 QuerySpec 路由到产品中心网关 HTTP。"""

from __future__ import annotations

from typing import Any

import structlog

from csagent.tools.semantic.models import FilterSpec, QuerySpec, SemanticQueryOutcome

from discount_label import enrich_discount_rows
from product_center_client import ProductCenterClient
from table_parser import parse_pipe_table

logger = structlog.get_logger(__name__)

# 一期网关已覆盖实体
_PC_ENTITIES = frozenset(
    {
        "fund_products",
        "fund_archive",
        "fund_risk_index",
        "fund_holdings",
        "fund_asset_scale",
        "fund_holders",
        "fund_dividends",
        "fund_invest_style",
        "fund_fee_rates",
        "fund_trade_limits",
        "fund_rate_discounts",
        "fund_open_periods",
        "fund_performance",
        "fund_quota_info",
        "fund_quota_control",
        "current_workday",
        "workdays",
        "fund_ta_info",
        "fund_mgmt_orgs",
    }
)


def supports_entity(entity: str) -> bool:
    return entity in _PC_ENTITIES


def _filter_eq(filters: list[FilterSpec], field: str) -> str | None:
    for flt in filters:
        if flt.field == field and flt.op == "eq" and flt.value is not None:
            return str(flt.value)
    return None


def _filter_like(filters: list[FilterSpec], field: str) -> str | None:
    for flt in filters:
        if flt.field == field and flt.op == "like" and flt.value is not None:
            return str(flt.value).strip("%").strip()
    return None


def _filter_name(filters: list[FilterSpec], field: str) -> str | None:
    """名称检索：优先 like，其次 eq（PC 侧均为模糊包含）。"""
    return _filter_like(filters, field) or _filter_eq(filters, field)


def _filter_in(filters: list[FilterSpec], field: str) -> list[str]:
    for flt in filters:
        if flt.field == field and flt.op == "in" and isinstance(flt.value, list):
            return [str(v) for v in flt.value if v is not None]
    return []


def _normalize_ymd(value: Any) -> str | None:
    """把日期规范成 YYYYMMDD，便于字符串比较。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return None


def _cmp_date(row_value: Any, op: str, bound: Any) -> bool:
    left = _normalize_ymd(row_value)
    right = _normalize_ymd(bound)
    if left is None or right is None:
        return False
    if op == "eq":
        return left == right
    if op == "gte":
        return left >= right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "lt":
        return left < right
    return True


def _apply_field_filters(
    rows: list[dict[str, Any]],
    filters: list[FilterSpec],
    fields: frozenset[str],
) -> list[dict[str, Any]]:
    """对指定字段应用 eq/in/like/gte/lte/gt/lt 本地过滤。"""
    active = [f for f in filters if f.field in fields]
    if not active:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        ok = True
        for flt in active:
            val = row.get(flt.field)
            if flt.op == "eq":
                if str(val or "") != str(flt.value):
                    ok = False
                    break
            elif flt.op == "in":
                allowed = {str(v) for v in (flt.value or []) if v is not None}
                if str(val or "") not in allowed:
                    ok = False
                    break
            elif flt.op == "like":
                needle = str(flt.value or "").strip("%").strip().lower()
                if needle and needle not in str(val or "").lower():
                    ok = False
                    break
            elif flt.op in {"gte", "lte", "gt", "lt"}:
                if not _cmp_date(val, flt.op, flt.value):
                    ok = False
                    break
            else:
                continue
        if ok:
            out.append(row)
    return out


_OPEN_PERIOD_DATE_FIELDS = frozenset({"trade_start_date", "trade_end_date"})
_FUND_LIST_EQ_FIELDS = frozenset(
    {
        "manager_code",
        "tacode",
        "consign_flag",
        "fund_type",
        "fundstate",
        "product_type",
    }
)


_STATUS_FIELDS = ("consign_flag", "buystatus", "redeemstatus", "fundstate")

# ---------- 对客宽表剔除的冗长文本（避免 2000 字工具截断把状态/限额挤掉） ----------
_VERBOSE_FUND_PRODUCT_FIELDS = frozenset(
    {
        "spcl_manage_rate_desc",
        "spcl_redeem_desc",
        "open_regulation",
        "redem_regulation",
        "profit_explain",
        "invest_object",
        "invest_strategy",
        "fund_desc",
        "fund_intro",
        "result_reward_remark",
    }
)

# 工具结果截断时优先保留的列（排在表头前部）
_PRIORITY_FUND_PRODUCT_COLS = (
    "fund_code",
    "fund_name",
    "custtype",
    "custtype_name",
    "consign_flag",
    "buystatus",
    "redeemstatus",
    "fundstate",
    "min_value",
    "max_value",
    "day_max_sum",
    "fund_type_name",
    "risky_name",
    "manager_name",
    "nav",
    "nav_update_date",
    "product_type",
    "redemption_cash_date",
    "redeem_ack_days",
    "redemption_settlement_guide",
    "hosting_rate",
    "service_rate",
    "manage_ratio",
)


def _ensure_status_fields(row: dict[str, Any]) -> None:
    """空/缺失的关键态字段显式写成空串，禁止下游缺键。"""
    for key in _STATUS_FIELDS:
        val = row.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            row[key] = ""


def _drop_verbose_fund_fields(row: dict[str, Any]) -> None:
    """剔除合同级长文本，保留费率数值与状态/限额关键字段。"""
    for key in _VERBOSE_FUND_PRODUCT_FIELDS:
        row.pop(key, None)


def _enrich_redemption_settlement(row: dict[str, Any]) -> None:
    """补充赎回确认/到账口径，避免模型把两个 T+N 串联叠加。"""
    cash_raw = row.get("redemption_cash_date")
    ack_raw = row.get("redeem_ack_days")
    cash = str(cash_raw).strip() if cash_raw is not None else ""
    notes: list[str] = []

    if ack_raw is not None and str(ack_raw).strip() != "":
        try:
            ack_days = int(float(ack_raw))
            notes.append(f"赎回份额确认：申请后 T+{ack_days}")
        except (TypeError, ValueError):
            notes.append(f"赎回份额确认：{ack_raw}")

    if cash:
        notes.append(
            f"赎回款到账：自申请日起计 {cash}（总到账周期，勿与赎回确认天数相加）"
        )

    if notes:
        row["redemption_settlement_guide"] = "；".join(notes)


def _expand_fund_detail_rows(
    data: dict[str, Any], *, custtype: str | None = None
) -> list[dict[str, Any]]:
    """将 trade_by_custtype 展平为个人/机构对等行，不偏向任一维度。"""
    channels = data.get("trade_by_custtype")
    base = {k: v for k, v in data.items() if k != "trade_by_custtype"}
    _ensure_status_fields(base)
    _enrich_redemption_settlement(base)
    _drop_verbose_fund_fields(base)
    if not isinstance(channels, list) or not channels:
        return [base]
    rows: list[dict[str, Any]] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        if custtype is not None and str(ch.get("custtype") or "") != custtype:
            continue
        row = dict(base)
        row.update(ch)
        _ensure_status_fields(row)
        _enrich_redemption_settlement(row)
        _drop_verbose_fund_fields(row)
        rows.append(row)
    return rows or [base]


def _ordered_columns(entity: str, rows: list[dict[str, Any]]) -> list[str]:
    """fund_products 把状态/限额列前置，降低工具结果截断误伤。"""
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    if entity != "fund_products":
        return seen
    head = [c for c in _PRIORITY_FUND_PRODUCT_COLS if c in seen]
    tail = [c for c in seen if c not in head]
    return head + tail


def _rows_to_text(entity: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"查询成功，实体 {entity} 无匹配数据。"
    cols = _ordered_columns(entity, rows)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    return f"查询成功，实体 {entity}，共 {len(rows)} 行：\n" + "\n".join(lines)


class ProductCenterQueryBackend:
    """按实体调用产品中心网关，返回 SemanticQueryOutcome。"""

    def __init__(self, client: ProductCenterClient) -> None:
        self._client = client

    async def execute(self, spec: QuerySpec) -> SemanticQueryOutcome:
        entity = spec.entity
        if not supports_entity(entity):
            return SemanticQueryOutcome(
                entity=entity,
                text=(
                    f"实体 {entity} 暂未接入产品中心网关。"
                    "请换用已支持实体（如 fund_products / fund_fee_rates / fund_performance）。"
                ),
            )
        try:
            rows = await self._fetch_rows(spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("product_center_query_failed", entity=entity, error=str(exc))
            return SemanticQueryOutcome(
                entity=entity,
                text=f"产品中心查询失败: {exc}",
            )
        text = _rows_to_text(entity, rows)
        return SemanticQueryOutcome(
            entity=entity,
            text=text,
            rows=parse_pipe_table(text),
        )

    async def _fetch_rows(self, spec: QuerySpec) -> list[dict[str, Any]]:
        entity = spec.entity
        filters = list(spec.filters or [])
        limit = spec.limit or 20

        if entity == "current_workday":
            return [await self._client.current_workday()]

        if entity == "workdays":
            one_date = _filter_eq(filters, "date")
            if one_date:
                return [await self._client.check_workday(one_date)]
            start = _filter_eq(filters, "start_date")
            end = _filter_eq(filters, "end_date")
            if not start or not end:
                raise RuntimeError(
                    "workdays 需要 start_date+end_date（eq）查区间，或 date（eq）判断单日"
                )
            return (await self._client.workday_list(start, end))[:limit]

        if entity == "fund_products":
            codes = _filter_in(filters, "fund_code")
            one = _filter_eq(filters, "fund_code")
            if one:
                codes = [one]
            custtype = _filter_eq(filters, "custtype")
            if codes:
                out: list[dict[str, Any]] = []
                # 未指定 custtype 时默认机构，与 multi_lookup / fims HEADER 对齐
                effective_cust = custtype if custtype is not None else "0"
                for code in codes:
                    data = await self._client.fund_detail(code, custtype=effective_cust)
                    out.extend(_expand_fund_detail_rows(data, custtype=effective_cust))
                    if len(out) >= limit:
                        break
                out = _apply_field_filters(out, filters, _FUND_LIST_EQ_FIELDS)
                return out[:limit]
            name = _filter_name(filters, "fund_name")
            list_filters = {
                field: _filter_eq(filters, field) for field in sorted(_FUND_LIST_EQ_FIELDS)
            }
            has_list_filter = any(list_filters.values())
            if not name and not has_list_filter:
                raise RuntimeError(
                    "fund_products 需要 fund_code（eq/in）、fund_name（eq/like），"
                    "或 manager_code/tacode/consign_flag/fund_type/fundstate/product_type"
                )
            rows = await self._client.search_funds(
                name or "",
                limit=max(limit * 3, limit),
                manager_code=list_filters.get("manager_code"),
                tacode=list_filters.get("tacode"),
                consign_flag=list_filters.get("consign_flag"),
                fund_type=list_filters.get("fund_type"),
                fundstate=list_filters.get("fundstate"),
                product_type=list_filters.get("product_type"),
            )
            # 双保险：网关未筛到的字段本地再滤一遍
            rows = _apply_field_filters(rows, filters, _FUND_LIST_EQ_FIELDS)
            return rows[:limit]

        if entity == "fund_archive":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_archive 需要 fund_code")
            return [await self._client.fund_archive(code)]

        if entity == "fund_risk_index":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_risk_index 需要 fund_code")
            return [await self._client.fund_risk_index(code)]

        if entity == "fund_holdings":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_holdings 需要 fund_code")
            data = await self._client.fund_holdings(code)
            rows: list[dict[str, Any]] = []
            for holding_type, key in (("stock", "stock_holdings"), ("bond", "bond_holdings")):
                for item in data.get(key) or []:
                    row = dict(item)
                    row.setdefault("fund_code", data.get("fund_code") or code)
                    row["holding_type"] = holding_type
                    rows.append(row)
            for item in data.get("asset_config_changes") or []:
                row = dict(item)
                row.setdefault("fund_code", data.get("fund_code") or code)
                row["holding_type"] = "asset_config"
                rows.append(row)
            holding_type_filter = _filter_eq(filters, "holding_type")
            if holding_type_filter:
                rows = [r for r in rows if r.get("holding_type") == holding_type_filter]
            return rows[:limit]

        if entity == "fund_asset_scale":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_asset_scale 需要 fund_code")
            return (await self._client.fund_asset_scale(code))[:limit]

        if entity == "fund_holders":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_holders 需要 fund_code")
            return (await self._client.fund_holders(code))[:limit]

        if entity == "fund_dividends":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_dividends 需要 fund_code")
            return (await self._client.fund_dividends(code))[:limit]

        if entity == "fund_invest_style":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_invest_style 需要 fund_code")
            return [await self._client.fund_invest_style(code)]

        if entity == "fund_fee_rates":
            code = _filter_eq(filters, "fund_code")
            codes = _filter_in(filters, "fund_code")
            if not code and not codes:
                raise RuntimeError("fund_fee_rates 需要 fund_code")
            if code:
                rows = await self._client.fee_rates(code)
            else:
                rows = await self._client.fee_rates_batch(codes[:10])
            custtype = _filter_eq(filters, "custtype")
            if custtype:
                rows = [r for r in rows if str(r.get("custtype") or "") == custtype]
            faretype = _filter_eq(filters, "faretype")
            if faretype:
                rows = [r for r in rows if str(r.get("faretype") or "") == faretype]
            return rows[:limit]

        if entity == "fund_trade_limits":
            code = _filter_eq(filters, "fund_code")
            codes = _filter_in(filters, "fund_code")
            if not code and not codes:
                raise RuntimeError("fund_trade_limits 需要 fund_code")
            if code:
                rows = await self._client.trade_limits(code)
            else:
                rows = await self._client.trade_limits_batch(codes[:10])
            custtype = _filter_eq(filters, "custtype")
            if custtype:
                rows = [r for r in rows if str(r.get("custtype") or "") == custtype]
            return rows[:limit]

        if entity == "fund_rate_discounts":
            code = _filter_eq(filters, "fund_code")
            codes = _filter_in(filters, "fund_code")
            if not code and not codes:
                raise RuntimeError("fund_rate_discounts 需要 fund_code")
            if code:
                rows = await self._client.discounts(code)
            else:
                rows = await self._client.discounts_batch(codes[:10])
            custtype = _filter_eq(filters, "custtype")
            if custtype:
                rows = [r for r in rows if str(r.get("custtype") or "") == custtype]
            return enrich_discount_rows(rows)[:limit]

        if entity == "fund_open_periods":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_open_periods 需要 fund_code")
            # 不传 busincode 时会合并查 020/022/024；传了则只查该业务码
            busin = _filter_eq(filters, "busincode")
            rows = await self._client.open_periods(code, busincode=busin)
            rows = _apply_field_filters(rows, filters, _OPEN_PERIOD_DATE_FIELDS)
            return rows[:limit]

        if entity == "fund_performance":
            code = _filter_eq(filters, "fund_code")
            codes = _filter_in(filters, "fund_code")
            if code:
                return [await self._client.performance(code)]
            if codes:
                out = []
                for c in codes[:limit]:
                    out.append(await self._client.performance(c))
                return out
            # 无代码：走排行（可按 fund_type 过滤；空则 bridge 合并 01/02/03/04）
            order_by = "thisyear_rose"
            direction = "desc"
            if spec.order_by and spec.order_by.field:
                order_by = spec.order_by.field
                direction = spec.order_by.direction or "desc"
            fund_type = _filter_eq(filters, "fund_type")
            return await self._client.ranking(
                order_by=order_by,
                direction=direction,
                limit=limit,
                fund_type=fund_type,
            )

        if entity == "fund_quota_info":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_quota_info 需要 fund_code")
            net_no = _filter_eq(filters, "net_no")
            try:
                row = await self._client.quota_info(code, net_no=net_no)
            except RuntimeError as exc:
                # 网关未升级时的兜底：产品中心「未查询到额度」不应打断主答复
                return [
                    {
                        "fund_code": code,
                        "net_no": net_no or "",
                        "quota_configured": False,
                        "note": (
                            "未查询到产品额度配置（公募通常不配额度；"
                            f"网关返回: {str(exc)[:160]}"
                        ),
                    }
                ]
            if isinstance(row, dict) and row.get("quota_configured") is False:
                return [row]
            return [row]

        if entity == "fund_quota_control":
            code = _filter_eq(filters, "fund_code")
            if not code:
                raise RuntimeError("fund_quota_control 需要 fund_code")
            net_no = _filter_eq(filters, "net_no")
            try:
                rows = await self._client.quota_controls(code, net_no=net_no)
            except RuntimeError:
                return []
            if net_no:
                rows = [r for r in rows if str(r.get("net_no") or "") == net_no]
            return rows[:limit]

        if entity == "fund_ta_info":
            code = _filter_eq(filters, "tacode") or _filter_eq(filters, "ta_code")
            if code:
                return [await self._client.ta_info(code)]
            name = _filter_name(filters, "taname") or _filter_name(filters, "ta_name")
            if name:
                return await self._client.search_ta(name, limit=limit)
            raise RuntimeError("fund_ta_info 需要 tacode（eq）或 taname（eq/like）")

        if entity == "fund_mgmt_orgs":
            code = _filter_eq(filters, "manager_code")
            if code:
                return [await self._client.manager_org(code)]
            name = _filter_name(filters, "manager_name")
            if name:
                return await self._client.search_managers(name, limit=limit)
            raise RuntimeError(
                "fund_mgmt_orgs 需要 manager_code（eq）或 manager_name（eq/like）"
            )

        raise RuntimeError(f"未实现实体路由: {entity}")
