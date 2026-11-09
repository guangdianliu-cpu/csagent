"""批量名称解析单元测试（对应 2026-08-25 的 27 只名单超时复盘）。"""

from __future__ import annotations

import asyncio
import json


from name_lookup_tool import (
    MAX_NAMES,
    RESULT_CHAR_BUDGET,
    match_candidate,
    run_name_lookup,
    split_share_class,
)


# ── stub client ──────────────────────────────────────────
class _StubClient:
    """按关键词返回预置候选，并记录调用次数与并发峰值。"""

    def __init__(self, table: dict[str, list[dict]], *, fail_on: set[str] | None = None):
        self._table = table
        self._fail_on = fail_on or set()
        self.calls: list[str] = []
        self.peak = 0
        self._live = 0

    async def search_funds(self, keyword: str = "", *, limit: int = 10, **_):
        self.calls.append(keyword)
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            await asyncio.sleep(0)
            if keyword in self._fail_on:
                raise RuntimeError("bridge 500")
            return list(self._table.get(keyword, []))[:limit]
        finally:
            self._live -= 1


def _fund(code: str, name: str, consign: str = "1") -> dict:
    return {"fund_code": code, "fund_name": name, "consign_flag": consign, "extra": "drop-me"}


# ── 份额类别拆分 ─────────────────────────────────────────
def test_split_share_class():
    assert split_share_class("华富恒盛纯债C") == ("华富恒盛纯债", "C")
    assert split_share_class("中银稳健增利A类") == ("中银稳健增利", "A")
    assert split_share_class("华商鸿盛纯债") == ("华商鸿盛纯债", "")
    assert split_share_class("") == ("", "")


# ── 候选匹配 ─────────────────────────────────────────────
def test_match_exact_and_share_class():
    cands = [_fund("006406", "华富恒盛纯债C"), _fund("006405", "华富恒盛纯债A")]
    v = match_candidate("华富恒盛纯债C", cands)
    assert v["match"] == "exact"
    assert v["row"]["fund_code"] == "006406"


def test_match_bare_name_hits_the_bare_share():
    cands = [_fund("015523", "华商鸿盛纯债"), _fund("015524", "华商鸿盛纯债C")]
    v = match_candidate("华商鸿盛纯债", cands)
    assert v["match"] == "exact"
    assert v["row"]["fund_code"] == "015523"


def test_match_bare_name_with_only_lettered_shares_is_ambiguous():
    # 用户写「长城短债」，库里只有 A/C —— 不许默认挑一只
    cands = [_fund("007194", "长城短债A"), _fund("007195", "长城短债C")]
    v = match_candidate("长城短债", cands)
    assert v["match"] == "ambiguous"


def test_match_ambiguous_when_share_class_missing():
    # 用户没写份额，候选又只有 A/C 两只 —— 不许替客户挑一只
    cands = [_fund("000001", "南方优选成长A"), _fund("000002", "南方优选成长C")]
    v = match_candidate("南方优选成长", cands)
    assert v["match"] == "ambiguous"
    assert v["row"] is None
    assert len(v["alternatives"]) == 2


def test_match_none_when_nothing_similar():
    v = match_candidate("查无此基", [_fund("000003", "完全不相干基金")])
    assert v["match"] == "none"
    assert v["row"] is None


# ── 端到端 ───────────────────────────────────────────────
def test_run_name_lookup_batches_in_one_call():
    table = {
        "华富恒盛纯债": [_fund("006406", "华富恒盛纯债C"), _fund("006405", "华富恒盛纯债A")],
        "华商鸿盛纯债": [_fund("015523", "华商鸿盛纯债")],
        "南方优选成长": [_fund("000001", "南方优选成长A"), _fund("000002", "南方优选成长C")],
        "查无此基": [],
    }
    client = _StubClient(table)
    out = json.loads(asyncio.run(run_name_lookup(
        client, ["华富恒盛纯债C", "华商鸿盛纯债", "南方优选成长C", "查无此基"]
    )))

    assert out["requested"] == 4
    assert out["resolved_count"] == 3
    assert out["fund_codes"] == ["006406", "015523", "000002"]
    # 名单核对只要这几列，别把搜索结果原样倒出来
    assert "extra" not in out["resolved"][0]
    assert out["resolved"][0]["consign_flag"] == "1"
    assert [u["query"] for u in out["unmatched"]] == ["查无此基"]
    # 搜索按基名去掉份额后缀，四个名字四次调用
    assert len(client.calls) == 4


def test_run_name_lookup_reports_ambiguous():
    table = {"南方优选成长": [_fund("000001", "南方优选成长A"), _fund("000002", "南方优选成长C")]}
    out = json.loads(asyncio.run(run_name_lookup(_StubClient(table), ["南方优选成长"])))
    assert out["resolved_count"] == 0
    assert out["ambiguous"][0]["query"] == "南方优选成长"
    assert len(out["ambiguous"][0]["candidates"]) == 2
    assert "ambiguous_note" in out


def test_run_name_lookup_single_failure_does_not_sink_the_batch():
    table = {"甲基金": [_fund("000010", "甲基金")], "乙基金": [_fund("000011", "乙基金")]}
    client = _StubClient(table, fail_on={"甲基金"})
    out = json.loads(asyncio.run(run_name_lookup(client, ["甲基金", "乙基金"])))
    assert out["fund_codes"] == ["000011"]
    assert "搜索失败" in out["unmatched"][0]["reason"]


def test_run_name_lookup_is_concurrent_and_capped():
    table = {f"基金{i}": [_fund(f"{i:06d}", f"基金{i}")] for i in range(20)}
    client = _StubClient(table)
    asyncio.run(run_name_lookup(client, [f"基金{i}" for i in range(20)]))
    assert client.peak > 1, "必须并发，串行就是线上超时的老路"
    assert client.peak <= 8


def test_run_name_lookup_truncates_loudly():
    names = [f"基金{i}" for i in range(MAX_NAMES + 3)]
    out = json.loads(asyncio.run(run_name_lookup(_StubClient({}), names)))
    assert out["requested"] == MAX_NAMES
    assert len(out["truncated"]) == 3
    assert "truncated_note" in out


def test_run_name_lookup_accepts_newline_string_and_rejects_empty():
    table = {"甲基金": [_fund("000010", "甲基金")]}
    out = json.loads(asyncio.run(run_name_lookup(_StubClient(table), "甲基金\n甲基金")))
    assert out["requested"] == 1  # 去重
    assert asyncio.run(run_name_lookup(_StubClient({}), [])).startswith("参数错误")


def test_match_ignores_terminated_funds():
    # 「易方达科瑞」同时命中在售混合型与一只已终止(fundstate=8)的封闭式
    cands = [
        _fund("003293", "易方达科瑞灵活配置混合"),
        dict(_fund("500056", "易方达科瑞封闭", consign="0"), fundstate="8"),
    ]
    v = match_candidate("易方达科瑞", cands)
    assert v["match"] == "share_class"
    assert v["row"]["fund_code"] == "003293"


def test_match_keeps_terminated_when_it_is_the_only_hit():
    cands = [dict(_fund("500056", "某已终止基金"), fundstate="8")]
    v = match_candidate("某已终止基金", cands)
    assert v["match"] == "exact"
    assert v["row"]["fund_code"] == "500056"


def test_large_batch_degrades_instead_of_being_cut_at_8000():
    """50 个名字的完整结果会撑破本工具预算，必须自己降级而不是丢出半截 JSON。"""
    # 贴近真实：搜索结果还带类型和管理人，实测每行约 210 字符
    table = {
        f"测试基金{i}号债券": [
            dict(
                _fund(f"{i:06d}", f"测试基金{i}号债券型证券投资基金C"),
                fund_type_name="债券型",
                manager_name=f"4{i}-测试基金管理有限公司",
                fundstate="0",
                buystatus="1",
            )
        ]
        for i in range(50)
    }
    names = [f"测试基金{i}号债券C" for i in range(50)]
    raw = asyncio.run(run_name_lookup(_StubClient(table), names))
    out = json.loads(raw)  # 关键：仍是合法 JSON

    assert len(raw) <= RESULT_CHAR_BUDGET
    assert out["view"] in ("compact", "minimal")
    assert out["resolved_count"] == 50      # 一个名字都没丢
    assert len(out["fund_codes"]) == 50
    assert "view_note" in out


def test_degradation_keeps_ambiguous_and_unmatched_intact():
    """resolved 可以压，ambiguous / unmatched 要如实转述给用户，不能压。"""
    table = {
        f"甲基金{i}": [
            dict(
                _fund(f"{i:06d}", f"甲基金{i}号债券型证券投资基金C"),
                fund_type_name="债券型",
                manager_name=f"4{i}-甲基金管理有限公司",
                fundstate="0",
                buystatus="1",
            )
        ]
        for i in range(45)
    }
    table["歧义基金"] = [_fund("900001", "歧义基金A"), _fund("900002", "歧义基金C")]
    names = [f"甲基金{i}C" for i in range(45)] + ["歧义基金", "查无此基"]

    out = json.loads(asyncio.run(run_name_lookup(_StubClient(table), names)))
    assert out.get("view") in ("compact", "minimal")
    assert out["ambiguous"][0]["query"] == "歧义基金"
    assert len(out["ambiguous"][0]["candidates"]) == 2   # 候选没被压掉
    assert out["unmatched"][0]["query"] == "查无此基"

