"""费率折扣字段解读：把 0/1/0.1 等原始值转成对客说明，避免模型自行换算。

折扣行有两个互相独立的因子，勿混用：
- ``discount``     → 只作用于费率档（``fund_fee_rates`` 里 ``flag=0``，按 ``fareratio``%）
- ``fix_discount`` → 只作用于单笔固定费用档（``flag=1``，按 ``farevalue`` 元/笔）
业务上允许两者取值不同（常见：费率打折、单笔固定费用不打折），
不得把 ``discount`` 套到单笔档上。

另外每行折扣只对 ``busincode`` 指定的那一项业务生效（020 认购 / 022 申购 / 024 赎回），
同一只基金的认购与申购折扣可以完全不同，勿跨业务套用。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# 折扣行的业务码 → 中文业务名。折扣按业务分行下发，认购/申购折扣常不一致，
# 模型无法从行内其他字段反推，必须由这里翻译出来。
BUSINCODE_NAMES = {
    "020": "认购",
    "022": "申购",
    "024": "赎回",
}

# discount / fix_discount 各自的作用范围，随行下发，避免把费率折扣套到单笔固定费用上。
DISCOUNT_SCOPE = (
    "discount 只打费率档（flag=0，按 fareratio%）；"
    "单笔固定费用档（flag=1，按 farevalue 元/笔）只看 fix_discount，两者互相独立"
)


def describe_discount(raw: Any) -> str:
    """discount / fix_discount 统一口径。

    - 1 → 不打折（按原费率）
    - 0 → 无费用（免收）
    - 其他 → N折（按原费率×因子），N = 因子×10（如 0.1→一折，0.01→0.1折）
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return f"折扣值={raw}"

    if value == 0:
        return "无费用（免收）"
    if value == 1:
        return "不打折（按原费率）"

    factor = _plain(value)
    zhe = _plain(value * Decimal(10))
    return f"{zhe}折（按原费率×{factor}）"


def describe_busincode(raw: Any) -> str:
    """折扣行的 busincode → 中文业务名；未知业务码原样带出，不猜。"""
    code = str(raw or "").strip()
    if not code:
        return ""
    return BUSINCODE_NAMES.get(code, f"其他业务({code})")


def enrich_discount_row(row: dict[str, Any]) -> dict[str, Any]:
    """为折扣行补齐 busincode_name / discount_desc / fix_discount_desc（已有则不覆盖）。"""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    if out.get("busincode") is not None and not out.get("busincode_name"):
        name = describe_busincode(out["busincode"])
        if name:
            out["busincode_name"] = name
    if not out.get("discount_scope") and (
        out.get("discount") is not None or out.get("fix_discount") is not None
    ):
        out["discount_scope"] = DISCOUNT_SCOPE
    if "discount" in out and out.get("discount") is not None and not out.get("discount_desc"):
        out["discount_desc"] = describe_discount(out["discount"])
    if (
        "fix_discount" in out
        and out.get("fix_discount") is not None
        and not out.get("fix_discount_desc")
    ):
        out["fix_discount_desc"] = describe_discount(out["fix_discount"])
    return out


def enrich_discount_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [enrich_discount_row(r) for r in rows]


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
