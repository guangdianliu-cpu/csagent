"""multi_fund_tool 复合查询单元测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from multi_fund_tool import (
    BATCH_CHUNK,
    CONCURRENCY,
    MAX_FUNDS,
    RESULT_CHAR_BUDGET,
    _pick_buy_limits,
    _slim,
    run_multi_fund_lookup,
)


# ── stub client ──────────────────────────────────────────
class _StubClient:
    """模拟 ProductCenterClient 的最小实现。"""

    def __init__(self, *, details=None, limits=None, fees=None, discounts=None,
                 archives=None, perfs=None):
        self._details = details or {}
        self._limits = limits or []
        self._fees = fees or []
        self._discounts = discounts or []
        self._archives = archives or {}
        self._perfs = perfs or {}

    async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
        data = self._details.get(fund_code, {"fund_code": fund_code, "fund_name": f"mock-{fund_code}"})
        row = dict(data)
        row.setdefault("custtype", custtype)
        row.setdefault("custtype_name", "机构" if custtype == "0" else "个人")
        return row

    async def trade_limits_batch(self, codes: list[str]):
        return [r for r in self._limits if r.get("fund_code") in codes]

    async def fee_rates_batch(self, codes: list[str]):
        return [r for r in self._fees if r.get("fund_code") in codes]

    async def discounts_batch(self, codes: list[str]):
        return [r for r in self._discounts if r.get("fund_code") in codes]

    async def fund_archive(self, fund_code: str):
        return self._archives.get(fund_code, {"fund_code": fund_code, "establish_date": "20010101"})

    async def performance(self, fund_code: str):
        return self._perfs.get(fund_code, {})


# ── helpers ──────────────────────────────────────────────
def test_slim_keeps_only_listed_columns():
    row = {"fund_code": "001", "fund_name": "test", "extra": 42}
    assert _slim(row, ("fund_code", "fund_name")) == {"fund_code": "001", "fund_name": "test"}


def test_slim_fills_missing_status_fields():
    row = {"fund_code": "001", "fund_name": "test"}
    slimmed = _slim(row, ("fund_code", "fund_name", "consign_flag", "buystatus"))
    assert slimmed["consign_flag"] == ""
    assert slimmed["buystatus"] == ""


def test_pick_buy_limits_prefers_022():
    rows = [
        {"fund_code": "A", "custtype": "1", "busincode": "020", "maxvalue": 100},
        {"fund_code": "A", "custtype": "1", "busincode": "022", "maxvalue": 200},
        {"fund_code": "A", "custtype": "1", "busincode": "024", "maxvalue": 300},
        {"fund_code": "A", "custtype": "0", "busincode": "022", "maxvalue": 500},
    ]
    picked = _pick_buy_limits(rows)
    assert len(picked) == 2
    by_cust = {str(r["custtype"]): r for r in picked}
    assert by_cust["1"]["busincode"] == "022"
    assert by_cust["0"]["busincode"] == "022"


# ── run_multi_fund_lookup ──────────────────────────────────


@pytest.mark.asyncio
async def test_basic_and_limits():
    client = _StubClient(
        details={
            "028232": {"fund_code": "028232", "fund_name": "长城C", "consign_flag": "1", "buystatus": "1"},
            "026511": {"fund_code": "026511", "fund_name": "工银A", "consign_flag": "1", "buystatus": "0"},
        },
        limits=[
            {"fund_code": "028232", "busincode": "022", "custtype": "0", "maxvalue": 9999999999},
            {"fund_code": "026511", "busincode": "022", "custtype": "0", "maxvalue": 100000000},
            {"fund_code": "028232", "busincode": "022", "custtype": "1", "maxvalue": 111},
        ],
    )
    result_str = await run_multi_fund_lookup(
        client, ["028232", "026511"], aspects=["basic", "limits"]
    )
    result = json.loads(result_str)
    assert result["custtype"] == "0"
    assert "028232" in result["funds"]
    assert "026511" in result["funds"]
    assert "basic" in result["funds"]["028232"]
    assert "limits" in result["funds"]["028232"]
    # 默认机构：不应混入个人限额行
    for lim in result["funds"]["028232"]["limits"]:
        assert str(lim.get("custtype")) == "0"


@pytest.mark.asyncio
async def test_custtype_personal_filters_limits():
    client = _StubClient(
        details={"028232": {"fund_code": "028232", "fund_name": "长城C", "consign_flag": "1"}},
        limits=[
            {"fund_code": "028232", "busincode": "022", "custtype": "0", "maxvalue": 999},
            {"fund_code": "028232", "busincode": "022", "custtype": "1", "maxvalue": 111},
        ],
    )
    result = json.loads(
        await run_multi_fund_lookup(
            client, ["028232"], custtype="1", aspects=["limits"]
        )
    )
    assert result["custtype"] == "1"
    assert result["funds"]["028232"]["limits"][0]["maxvalue"] == 111


@pytest.mark.asyncio
async def test_empty_codes_returns_error():
    result = await run_multi_fund_lookup(_StubClient(), [])
    assert "参数错误" in result


@pytest.mark.asyncio
async def test_invalid_aspect():
    result = await run_multi_fund_lookup(_StubClient(), ["001"], aspects=["nope"])
    assert "未知 aspect" in result


@pytest.mark.asyncio
async def test_deduplicates_codes():
    calls = []

    class _TrackClient(_StubClient):
        async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
            calls.append(fund_code)
            return {"fund_code": fund_code, "custtype": custtype}

    await run_multi_fund_lookup(_TrackClient(), ["A", "A", "B", "A"], aspects=["basic"])
    assert calls == ["A", "B"]


@pytest.mark.asyncio
async def test_all_four_aspects():
    client = _StubClient(
        details={"X": {"fund_code": "X", "fund_name": "test"}},
        limits=[{"fund_code": "X", "busincode": "022", "custtype": "0", "maxvalue": 100}],
        fees=[{"fund_code": "X", "faretype": "005", "custtype": "0", "fareratio": "0.15"}],
        discounts=[{"fund_code": "X", "custtype": "0", "net_no": "0001", "discount": "0.1"}],
    )
    result = json.loads(
        await run_multi_fund_lookup(
            client, ["X"], aspects=["basic", "limits", "fees", "discounts"]
        )
    )
    fund = result["funds"]["X"]
    assert "basic" in fund
    assert "limits" in fund
    assert "fees" in fund
    assert "discounts" in fund
    assert fund["discounts"][0].get("discount_desc") == "1折（按原费率×0.1）"


# ── 批量名单场景（2026-08-25 超时复盘）───────────────────
@pytest.mark.asyncio
async def test_basic_is_fetched_concurrently():
    """detail 只能按只查；串行 for 循环是 30 只名单卡到超时的直接原因。"""
    live = {"now": 0, "peak": 0}

    class _SlowClient(_StubClient):
        async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            try:
                await asyncio.sleep(0.01)
                return {"fund_code": fund_code, "custtype": custtype}
            finally:
                live["now"] -= 1

    codes = [f"{i:06d}" for i in range(16)]
    await run_multi_fund_lookup(_SlowClient(), codes, aspects=["basic"])
    assert live["peak"] > 1
    assert live["peak"] <= CONCURRENCY


@pytest.mark.asyncio
async def test_batch_endpoints_are_chunked():
    seen: list[int] = []

    class _ChunkClient(_StubClient):
        async def trade_limits_batch(self, codes: list[str]):
            seen.append(len(codes))
            return []

    codes = [f"{i:06d}" for i in range(25)]
    await run_multi_fund_lookup(_ChunkClient(), codes, aspects=["limits"])
    assert seen == [BATCH_CHUNK, BATCH_CHUNK, 5]


@pytest.mark.asyncio
async def test_over_cap_codes_are_reported_not_silently_dropped():
    codes = [f"{i:06d}" for i in range(MAX_FUNDS + 4)]
    result = json.loads(
        await run_multi_fund_lookup(_StubClient(), codes, aspects=["basic"])
    )
    assert len(result["funds"]) == MAX_FUNDS
    assert len(result["dropped"]) == 4
    assert "dropped_note" in result


@pytest.mark.asyncio
async def test_large_result_degrades_to_compact_not_broken_json():
    """超预算时必须自己降级；交给 run_script 硬切会切出坏 JSON。"""
    # 贴近真实 basic 行：字段多、每个都不长，整体撑破预算，精简后能塞回去
    details = {
        f"{i:06d}": {
            "fund_code": f"{i:06d}",
            "fund_name": f"测试基金{i}号债券C",
            "fund_full_name": f"测试基金{i}号债券型证券投资基金C类",
            "consign_flag": "1",
            "buystatus": "1",
            "fundstate": "0",
            "nav_display": "1.2009",
            "nav_update_date": "20260821",
            "fund_managers": "张三、李四",
            "manager_name": "测试基金管理有限公司",
            "redemption_settlement_guide": "赎回份额确认：申请后 T+1；赎回款到账：自申请日起计 T+2（总到账周期）",
            "risky_name": "中低风险",
        }
        for i in range(25)
    }
    raw = await run_multi_fund_lookup(
        _StubClient(details=details), list(details), aspects=["basic"]
    )
    result = json.loads(raw)  # 关键：仍然是合法 JSON
    assert result["view"] in ("compact", "minimal")
    assert result["count"] == 25          # 一只都没少
    assert len(raw) <= RESULT_CHAR_BUDGET
    assert "view_note" in result


@pytest.mark.asyncio
async def test_small_result_keeps_full_fidelity():
    result = json.loads(
        await run_multi_fund_lookup(
            _StubClient(details={"X": {"fund_code": "X", "fund_name": "test"}}),
            ["X"],
            aspects=["basic"],
        )
    )
    assert "view" not in result
    assert isinstance(result["funds"], dict)


# ── data_status 通用自描述（2026-09-05 空转复盘）────────────
@pytest.mark.asyncio
async def test_empty_values_self_describe():
    """字段返回但值全空 → 字段级 data_status 明说「空即暂无数据」。

    真实场景是混合块：成立日有值、业绩字段全空——只列空字段，不动有值字段。
    """
    client = _StubClient(
        details={"X": {"fund_code": "X", "fund_name": "test"}},
        perfs={"X": {}},
    )
    result = json.loads(
        await run_multi_fund_lookup(client, ["X"], aspects=["basic", "profile"])
    )
    note = result["data_status"]["profile"]
    assert "暂无数据" in note
    for f in ("year_rose", "year_ranking", "perf_workday"):
        assert f in note
    assert "establish_date" not in note  # 有值字段不受影响
    assert "暂无数据" in result["data_status_note"]


@pytest.mark.asyncio
async def test_no_records_self_describe():
    """某 aspect 所有基金都无记录 → 中性自描述。"""
    result = json.loads(
        await run_multi_fund_lookup(
            _StubClient(details={"X": {"fund_code": "X", "fund_name": "test"}}),
            ["X"],
            aspects=["basic", "discounts"],
        )
    )
    assert "未返回记录" in result["data_status"]["discounts"]


@pytest.mark.asyncio
async def test_with_data_no_self_description():
    """全部字段有真实数据时不得附注。"""
    client = _StubClient(
        details={"X": {"fund_code": "X", "fund_name": "test"}},
        perfs={"X": {
            "thisyear_rose": 3.2, "thisyear_ranking": 50,
            "year_rose": 12.5, "year_ranking": 100, "workday": "20260904",
        }},
    )
    result = json.loads(
        await run_multi_fund_lookup(client, ["X"], aspects=["profile"])
    )
    assert "data_status" not in result


@pytest.mark.asyncio
async def test_partially_null_no_blanket_note():
    """部分基金有数据、部分没有 → 字段级不触发（避免误伤）。"""
    client = _StubClient(
        details={"A": {"fund_code": "A"}, "B": {"fund_code": "B"}},
        perfs={
            "A": {
                "thisyear_rose": 3.2, "thisyear_ranking": 50,
                "year_rose": 5.0, "year_ranking": 80, "workday": "20260904",
            },
            "B": {},
        },
    )
    result = json.loads(
        await run_multi_fund_lookup(client, ["A", "B"], aspects=["profile"])
    )
    assert "data_status" not in result


# ── 接口调用失败 ≠ 暂无数据（网关故障时不能把「没查到」说成「空即暂无数据」）────
class _FlakyClient(_StubClient):
    """按代码 / 接口制造调用失败的 stub。"""

    def __init__(self, *, fail_detail=(), fail_batch=(), fail_perf=(), fail_archive=(), **kw):
        super().__init__(**kw)
        self._fail_detail = set(fail_detail)
        self._fail_batch = set(fail_batch)
        self._fail_perf = set(fail_perf)
        self._fail_archive = set(fail_archive)

    async def fund_detail(self, fund_code: str, *, custtype: str = "0"):
        if fund_code in self._fail_detail:
            raise RuntimeError(f"product-pc-bridge /api/funds/{fund_code} -> 502: bad gateway")
        return await super().fund_detail(fund_code, custtype=custtype)

    async def discounts_batch(self, codes: list[str]):
        if self._fail_batch & set(codes):
            raise RuntimeError("product-pc-bridge /api/discounts -> 504: timeout")
        return await super().discounts_batch(codes)

    async def fund_archive(self, fund_code: str):
        if fund_code in self._fail_archive:
            raise RuntimeError("archive down")
        return await super().fund_archive(fund_code)

    async def performance(self, fund_code: str):
        if fund_code in self._fail_perf:
            raise RuntimeError("perf down")
        return await super().performance(fund_code)


@pytest.mark.asyncio
async def test_failed_basic_is_marked_not_filled_with_empty_status():
    client = _FlakyClient(details={"A": {"fund_code": "A", "consign_flag": "1"}}, fail_detail={"B"})
    result = json.loads(await run_multi_fund_lookup(client, ["A", "B"], aspects=["basic"]))
    assert result["funds"]["A"]["basic"][0]["consign_flag"] == "1"
    assert "basic" not in result["funds"]["B"]  # 不再补一行空状态冒充数据
    assert result["funds"]["B"]["failed"] == ["basic"]
    assert result["failed"]["basic"]["codes"] == ["B"]
    assert "502" in result["failed"]["basic"]["error"]
    assert "不是「暂无数据」" in result["failed_note"]
    assert "basic" not in result.get("data_status", {})


@pytest.mark.asyncio
async def test_failed_batch_marks_only_its_chunk():
    codes = [f"{i:06d}" for i in range(BATCH_CHUNK + 2)]
    late = codes[BATCH_CHUNK:]  # 第二片
    client = _FlakyClient(
        discounts=[{"fund_code": c, "custtype": "0", "discount": "0.1"} for c in codes],
        fail_batch=set(late),
    )
    result = json.loads(await run_multi_fund_lookup(client, codes, aspects=["discounts"]))
    assert result["failed"]["discounts"]["codes"] == late
    assert "discounts" in result["funds"][codes[0]]  # 第一片照常返回
    assert result["funds"][late[0]]["failed"] == ["discounts"]


@pytest.mark.asyncio
async def test_failed_aspect_is_not_reported_as_no_records():
    """以前整项失败会被写成「接口调用成功但未返回记录——确无此类数据」。"""
    client = _FlakyClient(details={"X": {"fund_code": "X", "fund_name": "t"}}, fail_batch={"X"})
    result = json.loads(await run_multi_fund_lookup(client, ["X"], aspects=["basic", "discounts"]))
    assert result["failed"]["discounts"]["codes"] == ["X"]
    assert "discounts" not in result.get("data_status", {})


@pytest.mark.asyncio
async def test_all_failed_returns_upstream_failure_text():
    client = _FlakyClient(fail_detail={"A", "B"})
    text = await run_multi_fund_lookup(client, ["A", "B"], aspects=["basic"])
    assert text.startswith("多基金查询失败: 产品中心接口调用全部失败")
    assert "502" in text


@pytest.mark.asyncio
async def test_profile_half_failure_keeps_the_other_half():
    client = _FlakyClient(fail_perf={"X"})
    result = json.loads(await run_multi_fund_lookup(client, ["X"], aspects=["profile"]))
    assert result["funds"]["X"]["profile"]["establish_date"] == "20010101"
    assert result["funds"]["X"]["failed"] == ["profile"]
    assert "业绩接口" in result["failed"]["profile"]["error"]
    assert "data_status" not in result  # 业绩字段没查到，不能说成「值全为空」


@pytest.mark.asyncio
async def test_profile_both_halves_failed():
    client = _FlakyClient(details={"X": {"fund_code": "X"}}, fail_perf={"X"}, fail_archive={"X"})
    result = json.loads(await run_multi_fund_lookup(client, ["X"], aspects=["basic", "profile"]))
    assert "profile" not in result["funds"]["X"]
    assert result["funds"]["X"]["failed"] == ["profile"]


@pytest.mark.asyncio
async def test_compact_and_minimal_views_keep_failure_marks():
    details = {
        f"{i:06d}": {
            "fund_code": f"{i:06d}",
            "fund_name": f"测试基金{i}号债券C",
            "fund_full_name": f"测试基金{i}号债券型证券投资基金C类",
            "redemption_settlement_guide": "赎回份额确认：申请后 T+1；赎回款到账：自申请日起计 T+2（总到账周期）",
            "manager_name": "测试基金管理有限公司",
        }
        for i in range(29)
    }
    codes = [*details, "999999"]
    client = _FlakyClient(details=details, fail_detail={"999999"})
    result = json.loads(await run_multi_fund_lookup(client, codes, aspects=["basic"]))
    assert result["view"] in ("compact", "minimal")
    failed_rows = [r for r in result["funds"] if r.get("failed")]
    assert failed_rows == [{"fund_code": "999999", "failed": ["basic"]}]
    assert result["failed"]["basic"]["codes"] == ["999999"]


def test_compact_view_carries_data_status():
    """降级精简视图时自描述不能丢。"""
    from multi_fund_tool import _compact_result

    result = {
        "custtype": "0",
        "funds": {"X": {"basic": [{"fund_code": "X", "fund_name": "t"}], "profile": {
            "fund_code": "X", "year_rose": None, "year_ranking": None,
        }}},
        "data_status": {"profile": "值全为空"},
        "data_status_note": "请直接按「暂无数据」作答",
    }
    compact = _compact_result(result, ["basic", "profile"])
    assert compact["data_status"] == result["data_status"]
    assert compact["data_status_note"] == result["data_status_note"]

