"""产品中心语义查询执行器单测。"""

from __future__ import annotations

import asyncio
from typing import Any

from csagent.react.models import CustomerScope
from csagent.tools.semantic.catalog import QueryCatalog
from csagent.tools.semantic.executor import SemanticQueryExecutor

from product_center_backend import ProductCenterQueryBackend


class _FakeClient:
    async def fund_detail(self, fund_code: str, *, custtype: str = "0") -> dict[str, Any]:
        return {"fund_code": fund_code, "fund_name": "易方达消费", "nav": 3.45}

    async def fee_rates(self, fund_code: str) -> list[dict[str, Any]]:
        return [{"fund_code": fund_code, "faretype": "申购费", "fareratio": 0.15}]

    async def ranking(
        self, *, order_by: str, direction: str, limit: int, fund_type: str | None = None
    ):
        return [{"fund_code": "000001", "thisyear_rose": 10.0}][:limit]

    async def current_workday(self) -> dict[str, Any]:
        return {"workday": "20260716"}

    async def trade_limits(self, fund_code: str):
        return []

    async def discounts(self, fund_code: str):
        return []

    async def open_periods(self, fund_code: str):
        return []

    async def performance(self, fund_code: str):
        return {"fund_code": fund_code, "thisyear_rose": 1.0}

    async def quota_info(self, fund_code: str):
        return {"fund_code": fund_code}

    async def quota_controls(self, fund_code: str):
        return []

    async def ta_info(self, ta_code: str):
        return {"tacode": ta_code}

    async def manager_org(self, manager_code: str):
        return {"manager_code": manager_code}


def _executor() -> SemanticQueryExecutor:
    return SemanticQueryExecutor(
        catalog=QueryCatalog.load(),
        backend=ProductCenterQueryBackend(_FakeClient()),  # type: ignore[arg-type]
    )


def test_fund_products_query():
    ex = _executor()
    scope = CustomerScope(session_id="s1")
    text = asyncio.run(
        ex.execute(
            {
                "entity": "fund_products",
                "filters": [{"field": "fund_code", "op": "eq", "value": "110022"}],
            },
            scope,
        )
    )
    assert "110022" in text
    assert "易方达消费" in text


def test_invalid_filter_op():
    ex = _executor()
    scope = CustomerScope(session_id="s1")
    text = asyncio.run(
        ex.execute(
            {
                "entity": "fund_products",
                "filters": [{"field": "fund_code", "op": "like", "value": "11"}],
            },
            scope,
        )
    )
    assert "不支持操作符" in text or "校验" in text or "允许" in text
