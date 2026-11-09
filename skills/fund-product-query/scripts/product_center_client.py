"""产品中心 HTTP 网关客户端（product-pc-bridge）。"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class ProductCenterClient:
    """调用 services/product-pc-bridge 只读 API。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout_seconds
        headers = {"X-Bridge-Token": self._token} if self._token else {}
        # 把 Agent 这一轮的 trace / 会话带给 bridge，bridge 的接口审计记下它们，
        # 两边的审计才能按 trace_id 串成一条链（run_script 注入的 CSAGENT_* 变量）
        for header, env_name in (
            ("X-Csagent-Trace-Id", "CSAGENT_TRACE_ID"),
            ("X-Csagent-Session", "CSAGENT_SESSION_ID"),
        ):
            value = os.environ.get(env_name, "").strip()
            if value:
                headers[header] = value
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout_seconds,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"product-pc-bridge {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def health(self) -> dict[str, Any]:
        return await self._get("/api/health")

    async def fund_detail(self, fund_code: str, *, custtype: str = "0") -> dict[str, Any]:
        """查基金详情；custtype 默认 0=机构，1=个人。"""
        return await self._get(
            f"/api/funds/{fund_code}",
            params={"custtype": custtype or "0"},
        )

    async def list_by_main_fund(self, main_fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/by-main/{main_fund_code}")
        return list(data or [])

    async def search_funds(
        self,
        keyword: str = "",
        *,
        limit: int = 10,
        manager_code: str | None = None,
        tacode: str | None = None,
        consign_flag: str | None = None,
        fund_type: str | None = None,
        fundstate: str | None = None,
        product_type: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if keyword:
            params["q"] = keyword
        if manager_code:
            params["manager_code"] = manager_code
        if tacode:
            params["tacode"] = tacode
        if consign_flag:
            params["consign_flag"] = consign_flag
        if fund_type:
            params["fund_type"] = fund_type
        if fundstate:
            params["fundstate"] = fundstate
        if product_type:
            params["product_type"] = product_type
        data = await self._get("/api/funds", params=params)
        return list(data or [])

    async def fund_archive(self, fund_code: str) -> dict[str, Any]:
        return await self._get(f"/api/funds/{fund_code}/archive")

    async def fund_risk_index(self, fund_code: str) -> dict[str, Any]:
        return await self._get(f"/api/funds/{fund_code}/risk-index")

    async def fund_holdings(self, fund_code: str) -> dict[str, Any]:
        return await self._get(f"/api/funds/{fund_code}/holdings")

    async def fund_asset_scale(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/asset-scale")
        return list(data or [])

    async def fund_holders(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/holders")
        return list(data or [])

    async def fund_dividends(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/dividends")
        return list(data or [])

    async def fund_invest_style(self, fund_code: str) -> dict[str, Any]:
        return await self._get(f"/api/funds/{fund_code}/invest-style")

    async def fee_rates(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/fees")
        return list(data or [])

    async def fee_rates_batch(self, fund_codes: list[str]) -> list[dict[str, Any]]:
        codes = ",".join(c for c in fund_codes if c)
        if not codes:
            return []
        data = await self._get("/api/funds/batch/fees", params={"codes": codes})
        return list(data or [])

    async def trade_limits(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/trade-limits")
        return list(data or [])

    async def trade_limits_batch(self, fund_codes: list[str]) -> list[dict[str, Any]]:
        codes = ",".join(c for c in fund_codes if c)
        if not codes:
            return []
        data = await self._get("/api/funds/batch/trade-limits", params={"codes": codes})
        return list(data or [])

    async def discounts(self, fund_code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/funds/{fund_code}/discounts")
        return list(data or [])

    async def discounts_batch(self, fund_codes: list[str]) -> list[dict[str, Any]]:
        codes = ",".join(c for c in fund_codes if c)
        if not codes:
            return []
        data = await self._get("/api/funds/batch/discounts", params={"codes": codes})
        return list(data or [])

    async def open_periods(
        self, fund_code: str, *, busincode: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"busincode": busincode} if busincode else None
        data = await self._get(f"/api/funds/{fund_code}/open-periods", params=params)
        return list(data or [])

    async def performance(self, fund_code: str) -> dict[str, Any]:
        return await self._get(f"/api/funds/{fund_code}/performance")

    async def ranking(
        self,
        *,
        order_by: str = "thisyear_rose",
        direction: str = "desc",
        limit: int = 10,
        fund_type: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "order_by": order_by,
            "direction": direction,
            "limit": limit,
        }
        if fund_type:
            params["fund_type"] = fund_type
        data = await self._get("/api/funds/ranking", params=params)
        return list(data or [])

    async def quota_info(
        self, fund_code: str, *, net_no: str | None = None
    ) -> dict[str, Any]:
        params = {"net_no": net_no} if net_no else None
        return await self._get(f"/api/quota/{fund_code}", params=params)

    async def quota_controls(
        self, fund_code: str, *, net_no: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"net_no": net_no} if net_no else None
        data = await self._get(f"/api/quota/{fund_code}/controls", params=params)
        return list(data or [])

    async def current_workday(self) -> dict[str, Any]:
        return await self._get("/api/workday/current")

    async def workday_list(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/workday/list",
            params={"start": start_date, "end": end_date},
        )
        return list(data or [])

    async def check_workday(self, date: str) -> dict[str, Any]:
        return await self._get("/api/workday/check", params={"date": date})

    async def ta_info(self, ta_code: str) -> dict[str, Any]:
        return await self._get(f"/api/ta/{ta_code}")

    async def search_ta(self, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._get("/api/ta", params={"q": keyword, "limit": limit})
        return list(data or [])

    async def manager_org(self, manager_code: str) -> dict[str, Any]:
        return await self._get(f"/api/managers/{manager_code}")

    async def search_managers(
        self, keyword: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        data = await self._get("/api/managers", params={"q": keyword, "limit": limit})
        return list(data or [])
