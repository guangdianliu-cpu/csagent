"""批量基金名称 → 代码解析：一次调用解决整份名单，不要逐只 query。

背景（2026-08-25 线上超时复盘）：用户发来 27 只基金的名单/附件问「是否代销」，
名单里只有产品名没有代码。模型只能对每个名字单独发一次
``query(fund_products, fund_name like ...)``——25 次工具调用摊在 6~8 个 ReAct 轮次里，
光名称解析就吃掉 40 秒和一半轮次预算，最后撞上 ``max_rounds: 24`` / ``overall_timeout_seconds: 300``，
用户拿到的是「本次查询耗时过长」。

本模块把这一步压成 **一次** run_script：N 个名字并发打 ``/api/funds`` 搜索接口，
按份额类别对齐后返回 ``名字 → 代码`` 映射，模型拿到代码后再走一次 multi_lookup 即可成表。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

from product_center_client import ProductCenterClient

logger = structlog.get_logger(__name__)

# 一次最多解析多少个名字。名单类问题常见 20~30 只，50 足够覆盖，
# 再多说明用户是在传全量表格，应该让他先筛。
MAX_NAMES = 50

# 并发上限：产品中心是内网服务，8 路并发足以把 30 个名字压到 ~4 轮次往返，
# 又不至于把 bridge 打满。
CONCURRENCY = 8

# 单个名字取回多少候选。份额类别多的产品（A/C/D/E/I）10 条基本够。
DEFAULT_LIMIT = 10

# 搜索结果里带出来给模型的字段。多余字段会把 27 只的结果撑成几千 token，
# 名单核对阶段只要「是哪只、能不能买」。
_KEEP = (
    "fund_code", "fund_name", "fund_type_name",
    "manager_name", "consign_flag", "fundstate", "buystatus",
)

# 结果字符预算。和 multi_fund_tool 同源：run_script 会把 stdout 硬切到 8000
# 且切在 JSON 中间。50 个名字的完整结果实测 10655 字符，正好撞穿——
# 超预算时把每行压到「名字 → 代码」，够喂给下一步 multi_lookup 就行。
RESULT_CHAR_BUDGET = 7000
_COMPACT_KEEP = ("query", "match", "fund_code", "fund_name", "consign_flag")

# 已死的产品：3=发行失败 / 8=基金终止。名称对齐时先把它们排掉——
# 「易方达科瑞」同时命中在售的混合型和一只已终止的封闭式，不排掉就会误判成
# 「有多只分不清」而退回去问用户，而这只终止的根本不可能是他要问的。
_DEAD_FUNDSTATES = {"3", "8"}

# 份额类别后缀：结尾的单个大写字母（可带「类」字）。
# 「华富恒盛纯债C」→ base=华富恒盛纯债 / cls=C；「中银稳健增利A类」同理。
_SHARE_CLASS = re.compile(r"^(?P<base>.+?)(?P<cls>[A-EHIOR])类?$")


def split_share_class(name: str) -> tuple[str, str]:
    """拆出「基名 + 份额类别」。没有份额后缀时 cls 为空串。"""
    text = (name or "").strip()
    if not text:
        return "", ""
    m = _SHARE_CLASS.match(text)
    if not m:
        return text, ""
    return m.group("base").strip(), m.group("cls")


def _norm(text: Any) -> str:
    """比对用的归一化：去空白、去「类」、大写。"""
    return re.sub(r"\s+", "", str(text or "")).replace("类", "").upper()


def match_candidate(query_name: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """从搜索候选里挑出对应份额，返回 {match, row, alternatives}。

    match 取值（置信度从高到低）：
    - ``exact``       候选名与用户写法完全一致（归一化后比对，忽略空白与「类」字）
    - ``share_class`` 基名一致且份额类别一致，但整名写法不同
    - ``ambiguous``   基名对得上却分不出唯一份额，交回模型/人工判断——
                      用户没写 A/C 时**不替他挑**，这是「买错份额」级别的错
    - ``none``        没有任何候选对得上，``alternatives`` 给出最接近的几条供参考
    """
    rows = [c for c in candidates if isinstance(c, dict) and c.get("fund_code")]
    if not rows:
        return {"match": "none", "row": None, "alternatives": []}

    # 排掉已终止/发行失败的；全排光了就退回原集合，免得把「查到了但已终止」变成「查无此基」
    live = [r for r in rows if str(r.get("fundstate") or "") not in _DEAD_FUNDSTATES]
    rows = live or rows

    want = _norm(query_name)
    base, cls = split_share_class(query_name)
    want_base, want_cls = _norm(base), _norm(cls)

    exact = [r for r in rows if _norm(r.get("fund_name")) == want]
    if len(exact) == 1:
        return {"match": "exact", "row": exact[0], "alternatives": []}
    if len(exact) > 1:
        return {"match": "ambiguous", "row": None, "alternatives": exact}

    # 用户写的是简称，产品中心存的是全称——中间常多一截类型词：
    # 「华富恒盛纯债C」↔「华富恒盛纯债债券C」、「华商鸿盛纯债」↔「华商鸿盛纯债债券」。
    # 所以基名用「候选以用户写法开头」而不是全等来判。
    same_base = []
    for r in rows:
        cand_base, cand_cls = split_share_class(str(r.get("fund_name") or ""))
        if want_base and _norm(cand_base).startswith(want_base):
            same_base.append((r, _norm(cand_cls)))

    if same_base:
        if want_cls:
            # 用户写了份额类别 → 只认同类别的那只
            hit = [r for r, c in same_base if c == want_cls]
            if len(hit) == 1:
                return {"match": "share_class", "row": hit[0], "alternatives": []}
        else:
            # 用户没写份额 → 只认「候选也没有份额后缀」的那只；
            # 若候选全是 A/C/E，交回去问，随手挑一只等于替客户做决定
            bare = [r for r, c in same_base if not c]
            if len(bare) == 1:
                return {"match": "share_class", "row": bare[0], "alternatives": []}
        return {
            "match": "ambiguous",
            "row": None,
            "alternatives": [r for r, _ in same_base],
        }

    return {"match": "none", "row": None, "alternatives": rows[:5]}


def _slim_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {k: row[k] for k in _KEEP if k in row}


async def run_name_lookup(
    client: ProductCenterClient,
    fund_names: Any,
    *,
    limit: Any = None,
) -> str:
    """批量把基金名称解析成代码。成功返回 JSON 字符串，参数错误返回中文文案。

    payload 形状：``{"fund_names": ["华富恒盛纯债C", ...], "limit": 10}``
    """
    if isinstance(fund_names, str):
        fund_names = [n.strip() for n in re.split(r"[\n,，]", fund_names) if n.strip()]
    names = [str(n).strip() for n in (fund_names or []) if str(n or "").strip()]
    if not names:
        return "参数错误: fund_names 不能为空"

    names = list(dict.fromkeys(names))
    truncated = names[MAX_NAMES:]
    names = names[:MAX_NAMES]

    try:
        per_name_limit = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        per_name_limit = DEFAULT_LIMIT
    per_name_limit = max(1, min(per_name_limit, 30))

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(name: str) -> tuple[str, Any]:
        base, _cls = split_share_class(name)
        keyword = base or name
        async with sem:
            try:
                rows = await client.search_funds(keyword=keyword, limit=per_name_limit)
                return name, list(rows or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("name_lookup_search_failed", name=name, error=str(exc))
                return name, exc

    gathered = await asyncio.gather(*(one(n) for n in names))

    resolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for name, rows in gathered:
        if isinstance(rows, BaseException):
            unmatched.append({"query": name, "reason": f"搜索失败: {rows}"})
            continue
        verdict = match_candidate(name, rows)
        if verdict["match"] in ("exact", "share_class"):
            item = {"query": name, "match": verdict["match"]}
            item.update(_slim_row(verdict["row"]) or {})
            resolved.append(item)
        elif verdict["match"] == "ambiguous":
            ambiguous.append({
                "query": name,
                "candidates": [_slim_row(r) for r in verdict["alternatives"][:5]],
            })
        else:
            unmatched.append({
                "query": name,
                "reason": "未搜到同名产品",
                "near": [_slim_row(r) for r in verdict["alternatives"][:3]],
            })

    out: dict[str, Any] = {
        "requested": len(names),
        "resolved_count": len(resolved),
        "resolved": resolved,
        "fund_codes": [r["fund_code"] for r in resolved if r.get("fund_code")],
    }
    if ambiguous:
        out["ambiguous"] = ambiguous
        out["ambiguous_note"] = (
            "这些名字对应多只份额，未替用户挑选；请按 candidates 里的份额向用户确认，"
            "或在回复里说明存在多个份额"
        )
    if unmatched:
        out["unmatched"] = unmatched
    if truncated:
        out["truncated"] = truncated
        out["truncated_note"] = f"单次最多解析 {MAX_NAMES} 个名字，以上未处理"

    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) <= RESULT_CHAR_BUDGET:
        return text

    # 超预算：resolved 只留「名字 → 代码」。ambiguous / unmatched 是要如实转述给
    # 用户的，一个都不能压，所以只压 resolved。
    out["resolved"] = [
        {k: r[k] for k in _COMPACT_KEEP if k in r} for r in out["resolved"]
    ]
    out["view"] = "compact"
    out["view_note"] = (
        f"结果超过 {RESULT_CHAR_BUDGET} 字符，resolved 已精简为「名字→代码」；"
        "解析数量没变，一个名字都没丢，直接拿 fund_codes 进下一步即可"
    )
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) <= RESULT_CHAR_BUDGET:
        return text

    # 还超（名字特别长）：连 fund_name 也去掉，只剩 query + code
    out["resolved"] = [
        {"query": r.get("query"), "fund_code": r.get("fund_code")}
        for r in out["resolved"]
    ]
    out["view"] = "minimal"
    out["view_note"] = (
        f"结果超过 {RESULT_CHAR_BUDGET} 字符，resolved 只剩 query + fund_code；"
        "解析数量没变，产品全称改到下一步 product_lookup 的结果里看"
    )
    return json.dumps(out, ensure_ascii=False, default=str)
