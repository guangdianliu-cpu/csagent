"""同系列份额：模式开关与复合查询单元测试。"""

from __future__ import annotations

import json

import pytest

from share_siblings_tool import (
    run_share_siblings,
    should_lookup_siblings,
)


# ── mode helpers ─────────────────────────────────────────
def test_mode_off_never_looks_up():
    ok, reason = should_lookup_siblings("off", {"consign_flag": "0"})
    assert ok is False
    assert reason == "mode_off"


def test_consign_only_skips_when_consigned():
    ok, reason = should_lookup_siblings(
        "consign_only", {"consign_flag": "1", "buystatus": "0"}
    )
    assert ok is False
    assert reason == "already_consigned"


def test_consign_only_triggers_when_not_consigned():
    ok, reason = should_lookup_siblings("consign_only", {"consign_flag": "0"})
    assert ok is True
    assert reason == "not_consigned"


def test_unbuyable_triggers_on_closed_buy():
    ok, reason = should_lookup_siblings(
        "unbuyable",
        {
            "consign_flag": "1",
            "fundstate": "0",
            "trade_by_custtype": [
                {"custtype": "1", "buystatus": "0"},
                {"custtype": "0", "buystatus": "0"},
            ],
        },
    )
    assert ok is True
    assert reason == "buystatus_closed"


def test_unbuyable_skips_when_buyable():
    ok, reason = should_lookup_siblings(
        "unbuyable",
        {
            "consign_flag": "1",
            "fundstate": "0",
            "trade_by_custtype": [{"custtype": "1", "buystatus": "1"}],
        },
    )
    assert ok is False
    assert reason == "buyable"


def test_always_looks_up():
    ok, reason = should_lookup_siblings("always", {"consign_flag": "1", "buystatus": "1"})
    assert ok is True
    assert reason == "always"


# ── stub client ──────────────────────────────────────────
class _StubClient:
    def __init__(
        self,
        *,
        details=None,
        by_main=None,
        fees=None,
        discounts=None,
    ):
        self._details = details or {}
        self._by_main = by_main or []
        self._fees = fees or []
        self._discounts = discounts or []
        self.list_by_main_calls = 0

    async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
        return self._details.get(
            fund_code, {"fund_code": fund_code, "fund_name": f"mock-{fund_code}"}
        )

    async def list_by_main_fund(self, main_fund_code: str):
        self.list_by_main_calls += 1
        return list(self._by_main)

    async def fee_rates_batch(self, codes: list[str]):
        return [r for r in self._fees if r.get("fund_code") in codes]

    async def discounts_batch(self, codes: list[str]):
        return [r for r in self._discounts if r.get("fund_code") in codes]


# ── run_share_siblings ─────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_does_not_call_by_main():
    client = _StubClient(
        details={
            "000001": {
                "fund_code": "000001",
                "fund_main_code": "000001",
                "consign_flag": "0",
            }
        },
        by_main=[{"fund_code": "000002", "fund_name": "B"}],
    )
    raw = await run_share_siblings(client, "000001", mode="off")
    payload = json.loads(raw)
    assert payload["siblings"] == []
    assert payload["trigger_reason"] == "mode_off"
    assert client.list_by_main_calls == 0


@pytest.mark.asyncio
async def test_consign_only_skips_lookup_when_consigned():
    client = _StubClient(
        details={
            "000001": {
                "fund_code": "000001",
                "fund_main_code": "M1",
                "consign_flag": "1",
                "trade_by_custtype": [{"custtype": "1", "buystatus": "1"}],
            }
        },
        by_main=[{"fund_code": "000002"}],
    )
    raw = await run_share_siblings(client, "000001", mode="consign_only")
    payload = json.loads(raw)
    assert payload["siblings"] == []
    assert payload["trigger_reason"] == "already_consigned"
    assert client.list_by_main_calls == 0


@pytest.mark.asyncio
async def test_not_consigned_returns_siblings_with_fees():
    client = _StubClient(
        details={
            "000001": {
                "fund_code": "000001",
                "fund_name": "A类",
                "fund_main_code": "M1",
                "consign_flag": "0",
            },
            "000002": {
                "fund_code": "000002",
                "fund_name": "C类",
                "consign_flag": "1",
                "buystatus": "1",
            },
        },
        by_main=[
            {"fund_code": "000001", "fund_name": "A类"},
            {"fund_code": "000002", "fund_name": "C类"},
        ],
        fees=[{"fund_code": "000002", "faretype": "1", "fareratio": "0.015"}],
        discounts=[{"fund_code": "000002", "discount": "0.1", "custtype": "0"}],
    )
    raw = await run_share_siblings(client, "000001", mode="consign_only")
    payload = json.loads(raw)
    assert payload["trigger_reason"] == "not_consigned"
    assert payload["fund_main_code"] == "M1"
    assert len(payload["siblings"]) == 1
    sib = payload["siblings"][0]
    assert sib["fund_code"] == "000002"
    assert sib["basic"]
    assert sib["fees"]
    assert sib["discounts"]
    assert client.list_by_main_calls == 1


@pytest.mark.asyncio
async def test_empty_fund_code_returns_error():
    result = await run_share_siblings(_StubClient(), "")
    assert "参数错误" in result
