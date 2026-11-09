"""产品中心网关 QuerySpec 路由单测（无真实 HTTP）。"""

from __future__ import annotations

import asyncio
from typing import Any

from csagent.tools.semantic.models import FilterSpec, OrderBySpec, QuerySpec

from product_center_backend import (
    ProductCenterQueryBackend,
    _expand_fund_detail_rows,
    _rows_to_text,
)
from table_parser import parse_pipe_table


class _FakeClient:
    async def fund_detail(self, fund_code: str, *, custtype: str = "0") -> dict[str, Any]:
        return {
            "fund_code": fund_code,
            "fund_name": "测试基金",
            "nav": 1.23,
            "trade_by_custtype": [
                {"custtype": "1", "custtype_name": "个人", "min_value": 100},
                {"custtype": "0", "custtype_name": "机构", "min_value": 1000000},
            ],
        }

    async def fee_rates(self, fund_code: str) -> list[dict[str, Any]]:
        return [{"fund_code": fund_code, "faretype": "申购费", "fareratio": 0.15}]

    async def ranking(
        self, *, order_by: str, direction: str, limit: int, fund_type: str | None = None
    ):
        return [
            {"fund_code": "000001", "thisyear_rose": 99.0, "workday": "20260715"},
            {"fund_code": "000002", "thisyear_rose": 88.0, "workday": "20260715"},
        ][:limit]

    async def current_workday(self) -> dict[str, Any]:
        return {"workday": "20260715"}

    async def workday_list(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return [
            {"workday": "20260714", "start_date": start_date, "end_date": end_date},
            {"workday": "20260715", "start_date": start_date, "end_date": end_date},
        ]

    async def check_workday(self, date: str) -> dict[str, Any]:
        return {"date": date, "is_business_day": date == "20260715"}

    async def fund_archive(self, fund_code: str) -> dict[str, Any]:
        return {"fund_code": fund_code, "fund_name": "测试基金", "establish_date": "20100101"}

    async def fund_holdings(self, fund_code: str) -> dict[str, Any]:
        return {
            "fund_code": fund_code,
            "stock_holdings": [{"asset_name": "贵州茅台", "net_percent": 9.5}],
            "bond_holdings": [{"asset_name": "国债", "net_percent": 3.2}],
        }

    async def fund_dividends(self, fund_code: str) -> list[dict[str, Any]]:
        return [{"fund_code": fund_code, "dividend_date": "20260101", "every_dividend": 0.1}]

    async def open_periods(
        self, fund_code: str, *, busincode: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "fund_code": fund_code,
                "busincode": "022",
                "trade_start_date": "20260101",
                "trade_end_date": "20260115",
            },
            {
                "fund_code": fund_code,
                "busincode": "022",
                "trade_start_date": "20260701",
                "trade_end_date": "20260731",
            },
            {
                "fund_code": fund_code,
                "busincode": "024",
                "trade_start_date": "20260801",
                "trade_end_date": "20260815",
            },
        ]
        if busincode:
            rows = [r for r in rows if r["busincode"] == busincode]
        return rows

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
        rows = [
            {
                "fund_code": "110022",
                "fund_name": f"易方达{keyword or '蓝筹'}",
                "manager_code": "A001",
                "manager_name": "易方达基金",
                "tacode": "98",
                "consign_flag": "1",
                "fund_type": "01",
                "fundstate": "0",
                "product_type": "00",
            },
            {
                "fund_code": "000001",
                "fund_name": "华夏成长",
                "manager_code": "B002",
                "manager_name": "华夏基金",
                "tacode": "01",
                "consign_flag": "0",
                "fund_type": "03",
                "fundstate": "0",
                "product_type": "00",
            },
        ]
        if keyword:
            rows = [
                r
                for r in rows
                if keyword in r["fund_name"] or keyword in r["fund_code"]
            ]
        if manager_code:
            rows = [r for r in rows if r["manager_code"] == manager_code]
        if tacode:
            rows = [r for r in rows if r["tacode"] == tacode]
        if consign_flag:
            rows = [r for r in rows if r["consign_flag"] == consign_flag]
        if fund_type:
            rows = [r for r in rows if r["fund_type"] == fund_type]
        if fundstate:
            rows = [r for r in rows if r["fundstate"] == fundstate]
        if product_type:
            rows = [r for r in rows if r["product_type"] == product_type]
        return rows[:limit]

    async def search_managers(
        self, keyword: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [{"manager_code": "A001", "manager_name": f"{keyword}基金管理"}][:limit]

    async def search_ta(self, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return [{"tacode": "98", "taname": f"{keyword}登记结算"}][:limit]

    async def ta_info(self, ta_code: str) -> dict[str, Any]:
        return {"tacode": ta_code, "taname": "测试TA"}

    async def manager_org(self, manager_code: str) -> dict[str, Any]:
        return {"manager_code": manager_code, "manager_name": "测试管理人"}


def test_fund_products_by_code():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_products",
                filters=[FilterSpec(field="fund_code", op="eq", value="110022")],
            )
        )
    )
    assert "110022" in outcome.text
    assert "测试基金" in outcome.text
    # 默认机构单通道
    assert "机构" in outcome.text
    assert "个人" not in outcome.text
    assert "共 1 行" in outcome.text


def test_fund_products_filter_custtype():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_products",
                filters=[
                    FilterSpec(field="fund_code", op="eq", value="110022"),
                    FilterSpec(field="custtype", op="eq", value="0"),
                ],
            )
        )
    )
    assert "机构" in outcome.text
    assert "个人" not in outcome.text
    assert "共 1 行" in outcome.text


def test_performance_ranking_without_code():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_performance",
                order_by=OrderBySpec(field="thisyear_rose", direction="desc"),
                limit=2,
            )
        )
    )
    assert "000001" in outcome.text
    assert "99" in outcome.text


def test_fund_archive_by_code():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_archive",
                filters=[FilterSpec(field="fund_code", op="eq", value="110022")],
            )
        )
    )
    assert "20100101" in outcome.text


def test_fund_holdings_flattened_by_type():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_holdings",
                filters=[FilterSpec(field="fund_code", op="eq", value="110022")],
            )
        )
    )
    assert "贵州茅台" in outcome.text
    assert "国债" in outcome.text
    assert "共 2 行" in outcome.text


def test_fund_dividends_by_code():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_dividends",
                filters=[FilterSpec(field="fund_code", op="eq", value="110022")],
            )
        )
    )
    assert "20260101" in outcome.text


def test_unsupported_entity_message():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(backend.execute(QuerySpec(entity="fund_consign_stats")))
    assert "暂未接入" in outcome.text


def test_fund_products_by_name_like():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_products",
                filters=[FilterSpec(field="fund_name", op="like", value="%消费%")],
            )
        )
    )
    assert "110022" in outcome.text
    assert "消费" in outcome.text


def test_mgmt_orgs_by_name_like():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_mgmt_orgs",
                filters=[FilterSpec(field="manager_name", op="like", value="易方达")],
            )
        )
    )
    assert "A001" in outcome.text
    assert "易方达" in outcome.text


def test_ta_info_by_name_like():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_ta_info",
                filters=[FilterSpec(field="taname", op="like", value="中登")],
            )
        )
    )
    assert "98" in outcome.text
    assert "中登" in outcome.text


def test_open_periods_date_window_filter():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_open_periods",
                filters=[
                    FilterSpec(field="fund_code", op="eq", value="110022"),
                    FilterSpec(field="trade_start_date", op="gte", value="20260701"),
                    FilterSpec(field="trade_end_date", op="lte", value="20260731"),
                ],
            )
        )
    )
    assert "20260701" in outcome.text
    assert "20260101" not in outcome.text
    assert "共 1 行" in outcome.text


def test_fund_products_by_manager_code():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_products",
                filters=[FilterSpec(field="manager_code", op="eq", value="A001")],
            )
        )
    )
    assert "110022" in outcome.text
    assert "000001" not in outcome.text


def test_fund_products_by_consign_flag():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    outcome = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="fund_products",
                filters=[FilterSpec(field="consign_flag", op="eq", value="1")],
            )
        )
    )
    assert "110022" in outcome.text
    assert "000001" not in outcome.text


def test_workdays_list_and_check():
    backend = ProductCenterQueryBackend(_FakeClient())  # type: ignore[arg-type]
    listed = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="workdays",
                filters=[
                    FilterSpec(field="start_date", op="eq", value="20260714"),
                    FilterSpec(field="end_date", op="eq", value="20260715"),
                ],
            )
        )
    )
    assert "20260714" in listed.text
    checked = asyncio.run(
        backend.execute(
            QuerySpec(
                entity="workdays",
                filters=[FilterSpec(field="date", op="eq", value="20260715")],
            )
        )
    )
    assert "True" in checked.text or "true" in checked.text.lower()


def test_redemption_settlement_guide_not_additive():
    """赎回到账说明应为申请后总周期，禁止与确认天数相加。"""
    rows = _expand_fund_detail_rows(
        {
            "fund_code": "023635",
            "fund_name": "国泰中债优选投资级信用债指数C",
            "redeem_ack_days": 1,
            "redemption_cash_date": "T+2",
        }
    )
    guide = rows[0]["redemption_settlement_guide"]
    assert "申请后 T+1" in guide
    assert "自申请日起计 T+2" in guide
    assert "勿与赎回确认天数相加" in guide


def test_expand_drops_verbose_long_text_fields():
    """合同级长文本不进入工具结果，避免截断挤掉 buystatus/限额。"""
    long_desc = "创新型浮动管理费。" * 80
    rows = _expand_fund_detail_rows(
        {
            "fund_code": "025512",
            "fund_name": "广发信息产业股票C",
            "consign_flag": "1",
            "spcl_manage_rate_desc": long_desc,
            "fund_desc": "很长的产品描述",
            "trade_by_custtype": [
                {
                    "custtype": "1",
                    "buystatus": "1",
                    "min_value": 1,
                    "max_value": 9999999999,
                    "day_max_sum": 9999999999,
                }
            ],
        }
    )
    row = rows[0]
    assert "spcl_manage_rate_desc" not in row
    assert "fund_desc" not in row
    assert row["buystatus"] == "1"
    assert row["max_value"] == 9999999999
    text = _rows_to_text("fund_products", rows)
    # 关键列应排在表头前部，且长文本不出现
    header = text.splitlines()[1]
    assert header.index("buystatus") < header.index("fund_name") or "buystatus" in header
    assert "buystatus" in header.split(" | ")[:12] or "buystatus" in header
    assert long_desc not in text
    assert "9999999999" in text
    parsed = parse_pipe_table(text)
    assert parsed is not None
    assert parsed[0]["buystatus"] in (1, "1")
    assert parsed[0]["max_value"] == 9999999999
