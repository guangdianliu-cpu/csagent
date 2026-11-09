"""同主基金其他份额事实陈述。

按 PRODUCT_SHARE_SIBLING_MODE 决定是否查找同系列份额，
批量附带代销/状态/费率/折扣；仅陈述事实，不做投资建议。

由 skills/fund-product-query/scripts/query.py 的 share_siblings action 调用
（原 ShareSiblingsTool 已删除，编排逻辑保留为本模块的 run_share_siblings()）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from discount_label import enrich_discount_rows
from multi_fund_tool import (
    _BASIC_KEEP,
    _DISCOUNT_KEEP,
    _FEE_KEEP,
    _group_by_fund,
    _slim,
)
from product_center_backend import _expand_fund_detail_rows
from product_center_client import ProductCenterClient

logger = structlog.get_logger(__name__)

VALID_MODES = frozenset({"off", "consign_only", "unbuyable", "always"})
MAX_SIBLINGS = 8

# 停止申购 / 停止交易 / 发行失败 / 基金终止 / 封闭
_UNBUYABLE_FUNDSTATES = frozenset({"3", "4", "5", "8", "9"})
_BUY_OPEN = frozenset({"1", "y", "yes", "true"})


def _norm(val: Any) -> str:
    return str(val or "").strip()


def _consign_ok(detail: dict[str, Any]) -> bool:
    return _norm(detail.get("consign_flag")) == "1"


def _any_buy_open(detail: dict[str, Any]) -> bool:
    """根或 trade_by_custtype 任一渠道 buystatus 为可申购。"""
    root = _norm(detail.get("buystatus")).lower()
    if root in _BUY_OPEN:
        return True
    channels = detail.get("trade_by_custtype") or []
    if isinstance(channels, list):
        for ch in channels:
            if isinstance(ch, dict) and _norm(ch.get("buystatus")).lower() in _BUY_OPEN:
                return True
    return False


def should_lookup_siblings(
    mode: str, detail: dict[str, Any]
) -> tuple[bool, str]:
    """根据配置模式判断是否继续查同系列。返回 (是否查, trigger_reason)。"""
    m = (mode or "consign_only").strip().lower()
    if m not in VALID_MODES:
        m = "consign_only"
    if m == "off":
        return False, "mode_off"
    if m == "always":
        return True, "always"
    consign_ok = _consign_ok(detail)
    if m == "consign_only":
        if consign_ok:
            return False, "already_consigned"
        return True, "not_consigned"
    # unbuyable
    if not consign_ok:
        return True, "not_consigned"
    fundstate = _norm(detail.get("fundstate"))
    if fundstate in _UNBUYABLE_FUNDSTATES:
        return True, f"fundstate_{fundstate}"
    if not _any_buy_open(detail):
        return True, "buystatus_closed"
    return False, "buyable"


async def run_share_siblings(
    client: ProductCenterClient, fund_code: Any, *, mode: str = "consign_only"
) -> str:
    """同主基金其他份额：代销/状态/费率/折扣事实。

    按配置在未代销/不可申购等情况下返回同系列份额的代销、状态、费率、折扣；
    仅陈述事实，禁止投资建议或换品推荐。返回值：成功=JSON 字符串，失败=中文错误文案。
    """
    fund_code_norm = _norm(fund_code)
    if not fund_code_norm:
        return "参数错误: fund_code 不能为空"

    mode_norm = (mode or "consign_only").strip().lower()
    try:
        result = await _run(client, fund_code_norm, mode_norm)
    except Exception as exc:  # noqa: BLE001
        logger.warning("share_siblings_failed", fund_code=fund_code_norm, error=str(exc))
        return f"同系列份额查询失败: {exc}"

    return json.dumps(result, ensure_ascii=False, default=str)


async def _run(client: ProductCenterClient, fund_code: str, mode: str) -> dict[str, Any]:
    detail = await client.fund_detail(fund_code, custtype="0")
    if not isinstance(detail, dict):
        detail = {"fund_code": fund_code}

    asked = _slim_asked(detail, fund_code)
    ok, reason = should_lookup_siblings(mode, detail)
    out: dict[str, Any] = {
        "asked": asked,
        "siblings": [],
        "trigger_reason": reason,
        "mode": mode,
    }
    if not ok:
        return out

    main_code = _norm(detail.get("fund_main_code")) or fund_code
    out["fund_main_code"] = main_code
    series = await client.list_by_main_fund(main_code)
    sibling_codes: list[str] = []
    for row in series or []:
        code = _norm(row.get("fund_code"))
        if code and code != fund_code and code not in sibling_codes:
            sibling_codes.append(code)
    sibling_codes = sibling_codes[:MAX_SIBLINGS]
    if not sibling_codes:
        out["trigger_reason"] = reason + "|no_siblings"
        return out

    siblings = await _enrich_siblings(client, sibling_codes)
    out["siblings"] = siblings
    return out


def _slim_asked(detail: dict[str, Any], fund_code: str) -> dict[str, Any]:
    rows = _expand_fund_detail_rows(detail)
    if rows:
        slimmed = [_slim(r, _BASIC_KEEP) for r in rows]
        return {
            "fund_code": fund_code,
            "fund_main_code": _norm(detail.get("fund_main_code")) or fund_code,
            "basic": slimmed,
        }
    return {
        "fund_code": fund_code,
        "fund_main_code": _norm(detail.get("fund_main_code")) or fund_code,
        "consign_flag": detail.get("consign_flag"),
        "fundstate": detail.get("fundstate"),
        "buystatus": detail.get("buystatus"),
    }


async def _enrich_siblings(
    client: ProductCenterClient, codes: list[str]
) -> list[dict[str, Any]]:
    basic_task = _fetch_basic(client, codes)
    fees_task = client.fee_rates_batch(codes)
    disc_task = client.discounts_batch(codes)
    basic_rows, fee_rows, disc_rows = await asyncio.gather(
        basic_task, fees_task, disc_task, return_exceptions=True
    )

    by_basic = _group_by_fund(basic_rows) if isinstance(basic_rows, list) else {}
    by_fees = _group_by_fund(fee_rows) if isinstance(fee_rows, list) else {}
    by_disc = _group_by_fund(disc_rows) if isinstance(disc_rows, list) else {}

    out: list[dict[str, Any]] = []
    for code in codes:
        item: dict[str, Any] = {"fund_code": code}
        if code in by_basic:
            item["basic"] = [_slim(r, _BASIC_KEEP) for r in by_basic[code]]
        if code in by_fees:
            item["fees"] = [_slim(r, _FEE_KEEP) for r in by_fees[code]]
        if code in by_disc:
            item["discounts"] = [
                _slim(r, _DISCOUNT_KEEP) for r in enrich_discount_rows(by_disc[code])
            ]
        out.append(item)

    errors: dict[str, str] = {}
    if isinstance(basic_rows, BaseException):
        errors["basic"] = str(basic_rows)
    if isinstance(fee_rows, BaseException):
        errors["fees"] = str(fee_rows)
    if isinstance(disc_rows, BaseException):
        errors["discounts"] = str(disc_rows)
    if errors:
        for item in out:
            item.setdefault("errors", errors)
    return out


async def _fetch_basic(client: ProductCenterClient, codes: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in codes:
        try:
            data = await client.fund_detail(code, custtype="0")
            out.extend(_expand_fund_detail_rows(data, custtype="0"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("share_sibling_basic_skip", code=code, error=str(exc))
            out.append({"fund_code": code, "error": str(exc)})
    return out
