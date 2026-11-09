"""基金复合查询：一次调用取齐 basic/limits/fees/discounts。

单只或多只基金均可；默认机构口径（custtype=0），用户明确问个人再传 1。
内部并行取多维度，按基金预精简，返回紧凑 per-fund JSON。

由 skills/fund-product-query/scripts/query.py 的 multi_lookup action 调用
（原 MultiFundLookupTool 已删除，编排逻辑保留为本模块的 run_multi_fund_lookup()）。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog

from discount_label import enrich_discount_rows
from product_center_backend import _expand_fund_detail_rows
from product_center_client import ProductCenterClient

logger = structlog.get_logger(__name__)

VALID_ASPECTS = frozenset({"basic", "limits", "fees", "discounts", "profile"})
# 名单类问题常见 20~30 只（附件清单、群里贴一列产品名）。上限卡在 10 会逼模型
# 自己拆三批、再自己写脚本合并，2026-08-25 线上超时就是这么烧掉轮次的。
MAX_FUNDS = 30
# 批量接口按 codes= 逗号串走 GET，分片发出避免 URL 过长；分片之间并发。
BATCH_CHUNK = 10
# basic/profile 是按只查的接口，并发上限（内网 bridge，8 路够用又不打满）。
CONCURRENCY = 8

# 结果字符预算。run_script 会把子进程 stdout 硬切到 max_output_chars（当前 30000）——
# 切在 JSON 中间、虽有截断标记，模型拿到的仍是一段坏掉的 JSON。
# 23 只基金光 basic 就 1.9 万字符，aspects=all 更是 6 万，直接撞这个天花板。
# 所以超预算时由本工具**自己**降级成一行一只的精简视图，并说明降级了，
# 而不是把超大 JSON 丢出去让传输层拦腰砍断。
RESULT_CHAR_BUDGET = 7000

# 精简视图里每只基金保留的字段：名单核对要回答的就是「是哪只、代销吗、能买吗、买多少」。
_COMPACT_BASIC = (
    "fund_code", "fund_name", "product_type", "risky_type",
    "consign_flag", "fundstate", "buystatus", "redeemstatus",
)
_COMPACT_LIMITS = ("minvalue", "maxvalue", "day_max_sum")

# 再降一级：30 只时连精简视图也会破 8000，这一档只留「是哪只 + 能不能买」。
# failed 是「哪几项没查到」的标记，任何一档都不能丢。
_MINIMAL = ("fund_code", "fund_name", "consign_flag", "fundstate", "buystatus", "failed")

# 接口报错原文进结果时的长度上限（httpx / 网关的报错可能带一大段响应体）
_ERROR_CHARS = 200

_FAILED_NOTE = (
    "以上基金的这几项没查到：产品中心接口调用失败，不是「暂无数据」，也不能当成未代销或不可买。"
    "可以只对这些代码再调一次；仍失败就如实告诉用户这几项暂时查不到。已返回的其余数据照常作答"
)


def _block_rows(block: Any) -> list[dict[str, Any]]:
    """把 aspect 块归一成 dict 行列表（profile 是单 dict，其余是行列表）。"""
    if isinstance(block, dict):
        return [block]
    if isinstance(block, list):
        return [r for r in block if isinstance(r, dict)]
    return []


def _empty_value_aspects(result: dict[str, Any], aspects: list[str]) -> dict[str, str]:
    """通用自描述：识别「接口调通但数据是空的」并当场明说。

    动机（2026-09-05 线上复盘）：null 是模糊信号，模型无法区分「数据源没有」
    和「我查错了」，会默认后者并换实体/换接口/写脚本重试，全部空转（实测同一
    问题 25 轮打满；纯文档约定 19/25/9 轮，压不住）。在**响应里**把「没有」
    明说出来，模型当场即可收口——实测降到 3 轮。

    通用原则（对齐 SKILL.md 核心口径）：接口调通 + 字段在返回里 = 数据可靠，
    值是什么就是什么。判定是**字段级**的，不针对任何业务场景：
    - 某字段在所有基金行里都存在、值全空 → 列出该字段并说明「空即暂无数据」
      （同一 aspect 里其余有值的字段不受影响，如成立日有值、业绩为空）
    - 某 aspect 所有基金都没拿到记录 → 「无记录本身就是有意义的答案」
    只在**所有**基金都空时才触发；部分有数据不附注（不误伤）。
    """
    funds: dict[str, Any] = result.get("funds") or {}
    status: dict[str, str] = {}
    if not funds:
        return status
    for a in aspects:
        blocks = [(blk or {}).get(a) for blk in funds.values()]
        # 档 2：所有基金都没拿到该块的记录
        if all(not b for b in blocks):
            status[a] = "接口调用成功但未返回记录——该口径下确无此类数据，这本身就是完整答案"
            continue
        if any(not b for b in blocks):
            continue  # 部分基金缺块，情况混合，不妄下结论
        rows_per_fund = [_block_rows(b) for b in blocks]
        if not all(rows_per_fund):
            continue
        common_keys = set.intersection(
            *(set(r) for rows in rows_per_fund for r in rows)
        ) - {"fund_code", "error"}
        null_fields = sorted(
            k for k in common_keys
            if all(r.get(k) in (None, "", []) for rows in rows_per_fund for r in rows)
        )
        if null_fields:
            status[a] = (
                "接口调用成功，以下字段已返回但值全为空——数据可靠，空即「暂无数据」，"
                "不是查询方式或参数问题：" + "、".join(null_fields)
            )
    return status


def resolve_out_path(out_file: Any) -> Path | None:
    """把 out_file 落到本会话的 outbound 目录。

    run_script 起子进程时已经注入 CSAGENT_OUTBOUND_DIR（见 script_pack._build_scope_env），
    所以模型只要给个裸文件名，不需要知道 session id，也不该自己拼 data/uploads/... 路径。
    只取 basename：不允许用 ../ 写到工作区外面。
    """
    name = str(out_file or "").strip()
    if not name:
        return None
    name = Path(name.replace("\\", "/")).name
    if not name or name.startswith("."):
        return None
    if not name.lower().endswith(".json"):
        name += ".json"
    base = os.environ.get("CSAGENT_OUTBOUND_DIR") or "."
    out_dir = Path(base)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / name
DEFAULT_CUSTTYPE = "0"  # 默认机构，对齐 fims HEADER / 客服主场景

# ── per-aspect 只保留对客有意义的字段 ──────────────────────
_BASIC_KEEP = (
    "fund_code", "fund_name", "fund_full_name", "fund_type_name",
    "fund_sub_type_name",
    "product_type", "fundstate", "consign_flag",
    "buystatus", "redeemstatus",
    "nav", "nav_display", "nav_update_date",
    # 日涨幅 / 规模 / 基金经理 —— 同一次 fund_detail 就带回来了，
    # 以前被这张白名单挡掉，导致模型得再多跳几次去别的实体取，纯浪费。
    "day_rose",
    "asset_amount", "asset_report_date",
    "fund_managers",
    "custtype", "custtype_name",
    "min_value", "max_value", "day_max_sum",
    "buy_ack_days", "redeem_ack_days", "sub_ack_days",
    "redemption_cash_date", "redemption_settlement_guide",
    "manager_name", "risky_type", "risky_name",
    "hosting_rate", "service_rate", "manage_ratio",
)

# profile：成立日（archive）+ 近一年涨幅/同类排名（performance）。
# 这两样 fund_detail 不带，但做「产品卡片」式回答时几乎必用，
# 单独成一个 aspect，避免模型为了两个字段再起两轮 run_script。
_PROFILE_KEEP = (
    "fund_code", "establish_date",
    "thisyear_rose", "thisyear_ranking",
    "year_rose", "year_ranking", "year_total_fund_num",
    "perf_workday",
)

# performance.periods[] 里按时段分的排名分母。顶层只有名次没有总数，
# 「同类 1405/8270」的 8270 只能从这里取。02=近1周 06=近1年，见接口返回。
_PERIOD_YEAR = "06"

_LIMIT_KEEP = (
    "fund_code", "busincode", "sharetype", "custtype",
    "trusttype", "trade_flag",
    "minvalue", "maxvalue", "custmaxvalue", "day_max_sum",
)

_FEE_KEEP = (
    "fund_code", "faretype", "custtype", "fareratio", "farevalue",
    "minvalue", "maxvalue", "flag", "unit",
)

_DISCOUNT_KEEP = (
    "fund_code", "custtype", "net_no",
    "discount", "discount_desc",
    "fix_discount", "fix_discount_desc",
    # busincode_name/discount_scope 由 discount_label 补出：折扣按业务分行，
    # 且费率折扣与单笔固定费用折扣互相独立，缺了这两个字段模型只能靠猜。
    "busincode", "busincode_name", "discount_scope",
    "agencyfareratio", "agency_fee_model",
)


def _normalize_custtype(raw: Any) -> str:
    """0=机构 / 1=个人；空或非法回落机构。"""
    text = str(raw or "").strip()
    if text in {"1", "personal", "个人"}:
        return "1"
    return DEFAULT_CUSTTYPE


def _slim(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, Any]:
    out = {k: row[k] for k in cols if k in row}
    # 关键状态缺键时补空串，避免模型把「看不到字段」误判为代销/可买
    for k in ("consign_flag", "buystatus", "redeemstatus", "fundstate"):
        if k in cols and k not in out:
            out[k] = ""
    return out


def _group_by_fund(rows: list[dict[str, Any]], code_field: str = "fund_code") -> dict[str, list]:
    out: dict[str, list] = {}
    for r in rows:
        code = str(r.get(code_field) or "").strip()
        if code:
            out.setdefault(code, []).append(r)
    return out


def _filter_custtype(rows: list[dict[str, Any]], custtype: str) -> list[dict[str, Any]]:
    """批量接口常双返回；按请求口径过滤。无 custtype 字段的行保留。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get("custtype")
        if raw is None or str(raw).strip() == "":
            out.append(row)
            continue
        if str(raw).strip() == custtype:
            out.append(row)
    return out


# ── 申购限额精简：每个 fund × custtype 只保留 busincode=022 (申购) ──
_BUY_BUSINCODES = ("022", "020", "024")


def _pick_buy_limits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个 fund_code × custtype 只留一行最有意义的申购限额。"""
    by_key: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(r.get("fund_code") or "")
        cust = str(r.get("custtype") or "")
        key = f"{code}|{cust}"
        busin = str(r.get("busincode") or "")
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = r
        elif busin in _BUY_BUSINCODES:
            old_busin = str(existing.get("busincode") or "")
            if old_busin not in _BUY_BUSINCODES or _BUY_BUSINCODES.index(busin) < _BUY_BUSINCODES.index(old_busin):
                by_key[key] = r
    return list(by_key.values())


async def run_multi_fund_lookup(
    client: ProductCenterClient,
    fund_codes: Any,
    *,
    custtype: Any = None,
    aspects: Any = None,
    out_file: Any = None,
) -> str:
    """基金一站式查询：基本面 + 限额 + 费率 + 折扣（含确认日/交收）。

    单只或多只均可；默认 custtype=0 机构，用户明确问个人时传 custtype=1；
    代销/限额/费率/折扣/申购确认/赎回到账优先用本函数；开放期/额度/档案/业绩等走
    executor.py 的结构化查询。返回值：成功=JSON 字符串，失败=中文错误文案。
    """
    if isinstance(fund_codes, str):
        fund_codes = [c.strip() for c in fund_codes.split(",") if c.strip()]
    fund_codes = list(fund_codes or [])
    if not fund_codes:
        return "参数错误: fund_codes 不能为空"
    fund_codes = list(dict.fromkeys(fund_codes))
    dropped = fund_codes[MAX_FUNDS:]
    fund_codes = fund_codes[:MAX_FUNDS]

    custtype_norm = _normalize_custtype(custtype)

    # 省略 = 四项常用（不含 profile：它每只多 2 次 HTTP，简单问题不该付这个成本）。
    # 做完整「产品卡片」时传 "all" 一次取齐，省掉模型再起一轮 run_script。
    aspects = aspects or ["basic", "limits", "fees", "discounts"]
    if isinstance(aspects, str):
        aspects = [a.strip() for a in aspects.split(",") if a.strip()]
    if len(aspects) == 1 and str(aspects[0]).lower() == "all":
        aspects = sorted(VALID_ASPECTS)
    invalid = set(aspects) - VALID_ASPECTS
    if invalid:
        return f"参数错误: 未知 aspect {invalid}，可选: {sorted(VALID_ASPECTS)}"

    try:
        result, failures = await _fetch_all(client, fund_codes, aspects, custtype_norm)
    except Exception as exc:
        logger.warning("multi_fund_lookup_failed", error=str(exc))
        return f"多基金查询失败: {exc}"

    # 接口调用失败必须和「接口调通但没数据」分开：以前失败的基金照样补出空状态字段、
    # 再配一句「数据可靠，空即暂无数据」，网关一挂，模型就把「查不到」答成「暂无数据」。
    if failures:
        if _all_failed(result, failures, fund_codes, aspects):
            detail = "；".join(
                f"{a}: {_failure_view(failures[a], fund_codes)['error']}" for a in aspects if a in failures
            )
            logger.warning("multi_fund_lookup_all_failed", detail=detail)
            return f"多基金查询失败: 产品中心接口调用全部失败（{detail}）"
        result["failed"] = {a: _failure_view(failures[a], fund_codes) for a in aspects if a in failures}
        result["failed_note"] = _FAILED_NOTE

    # 「接口调通但数据为空」必须当场自描述：光靠 SKILL.md 的静态原则压不住模型
    # 对 null 的怀疑（实测 19/25/9 轮空转），响应内的即时信号才能让它收口（3 轮）。
    # 有调用失败的 aspect 不下「空即暂无数据」的结论。
    empty_status = _empty_value_aspects(result, [a for a in aspects if a not in failures])
    if empty_status:
        result["data_status"] = empty_status
        result["data_status_note"] = (
            "以上 aspect 接口调用成功，空值就是数据的真实状态：请直接按「暂无数据」如实作答，"
            "不要换实体、换接口或写脚本另查——那些路径取的是同一数据源，结果相同"
        )

    # 超限的部分必须说出来：静默截断会让模型以为整份名单都查过了，
    # 直接给客户一张少了几只的表。
    if dropped:
        result["dropped"] = dropped
        result["dropped_note"] = (
            f"单次最多查 {MAX_FUNDS} 只，以上代码本次未查询，请对剩余部分再调一次"
        )

    text = json.dumps(result, ensure_ascii=False, default=str)

    # out_file：完整结果落盘给出表用，stdout 只回精简视图 + 路径。
    # 上下文里永远只有摘要，多大的名单都不会再撞 8000。
    if str(out_file or "").strip():
        # 目录创建和写入都可能抛 OSError（目录不可写、盘满）。整块兜住：
        # 落盘失败不该把整次查询变成「脚本执行异常」，数据已经在手上了。
        try:
            out_path = resolve_out_path(out_file)
            if out_path is None:
                raise OSError(f"非法的 out_file: {out_file!r}")
            out_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("multi_fund_out_file_failed", out_file=str(out_file), error=str(exc))
            result["out_file_error"] = (
                f"落盘失败: {exc}。结果仍在下方，可直接作答；不要为此重查"
            )
        else:
            result["saved_to"] = out_path.name
            result["saved_chars"] = len(text)
            result["saved_note"] = (
                f"完整结果已写入会话 outbound 目录的 {out_path.name}；"
                "出表用 product_make_report(source=该文件名)，发给用户用 send_file(报表文件名)"
            )
        compact = _compact_result(result, aspects)
        for k in ("saved_to", "saved_chars", "saved_note", "out_file_error"):
            if k in result:
                compact[k] = result[k]
        return json.dumps(compact, ensure_ascii=False, default=str)

    if len(text) <= RESULT_CHAR_BUDGET:
        return text

    compact = _compact_result(result, aspects)
    text = json.dumps(compact, ensure_ascii=False, default=str)
    if len(text) <= RESULT_CHAR_BUDGET:
        return text

    # 30 只时精简视图也会超预算，再降一级到最小字段集
    compact["funds"] = [
        {k: row[k] for k in _MINIMAL if k in row} for row in compact["funds"]
    ]
    compact["view"] = "minimal"
    compact["view_note"] = (
        f"结果超过 {RESULT_CHAR_BUDGET} 字符，已降到最小视图：每只只留代码/名称/代销/状态。"
        "数据完整、没有基金被丢掉；需要净值、限额等字段时把 fund_codes 拆成两批再查"
    )
    return json.dumps(compact, ensure_ascii=False, default=str)


def _compact_result(result: dict[str, Any], aspects: list[str]) -> dict[str, Any]:
    """超预算时的降级视图：一只一行，只留名单核对用得上的字段。

    宁可主动降级并说清楚，也不能让 run_script 把 JSON 从中间切断——
    后者模型无从判断少了哪几只，只会照着残缺数据答。
    """
    rows: list[dict[str, Any]] = []
    for code, blk in (result.get("funds") or {}).items():
        row: dict[str, Any] = {"fund_code": code}
        basic = (blk.get("basic") or [{}])[0]
        for k in _COMPACT_BASIC:
            if k in basic:
                row[k] = basic[k]
        limits = (blk.get("limits") or [{}])[0]
        for k in _COMPACT_LIMITS:
            if k in limits:
                row[k] = limits[k]
        if blk.get("failed"):
            row["failed"] = blk["failed"]
        rows.append(row)

    omitted = [a for a in ("fees", "discounts", "profile") if a in aspects]
    out: dict[str, Any] = {
        "custtype": result.get("custtype"),
        "view": "compact",
        "view_note": (
            f"结果超过 {RESULT_CHAR_BUDGET} 字符，已降级为精简视图：每只基金一行，"
            "字段够回答代销/可买/限额类问题。数据本身是完整的，没有基金被丢掉"
        ),
        "count": len(rows),
        "funds": rows,
    }
    if omitted:
        out["omitted_aspects"] = omitted
        out["omitted_note"] = (
            f"{'/'.join(omitted)} 明细未随本次返回；确需这些字段时，"
            "把 fund_codes 缩小到 5 只以内再查一次"
        )
    for k in ("dropped", "dropped_note", "failed", "failed_note", "data_status", "data_status_note"):
        if k in result:
            out[k] = result[k]
    return out


def _all_failed(
    result: dict[str, Any], failures: dict[str, dict[str, str]], fund_codes: list[str], aspects: list[str]
) -> bool:
    """每只基金的每一项都调用失败、且手上一点数据都没有，才算整次失败。"""
    every_pair = all(code in failures.get(a, {}) for a in aspects for code in fund_codes)
    funds = result.get("funds") or {}
    has_data = any(k != "failed" for blk in funds.values() for k in blk)
    return every_pair and not has_data


def _failure_view(failed: dict[str, str], order: list[str]) -> dict[str, Any]:
    """某一项没查到的基金代码，以及一条有代表性的接口报错原文。

    报错里常带各自的接口路径（/api/funds/<代码>），逐只列出既占预算又没多少信息，
    只给第一只的原文，报错不止一种时注明。
    """
    codes = [c for c in order if c in failed]
    error = str(failed[codes[0]])[:_ERROR_CHARS]
    kinds = len({str(failed[c]) for c in codes})
    if kinds > 1:
        error += f"（共 {kinds} 种报错，此为 {codes[0]} 的）"
    return {"codes": codes, "error": error}


async def _fetch_all(
    client: ProductCenterClient, fund_codes: list[str], aspects: list[str], custtype: str
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """并行取各 aspect，返回 ``(结果, 调用失败)``。

    调用失败按 ``{aspect: {fund_code: 报错}}`` 记，和「接口调通但没有记录」严格分开：
    失败的基金不出该 aspect 的块，并在自己的 ``failed`` 里点名是哪几项。
    """
    tasks: dict[str, Any] = {}
    if "basic" in aspects:
        tasks["basic"] = _fetch_basic(client, fund_codes, custtype)
    if "limits" in aspects:
        tasks["limits"] = _batched(client.trade_limits_batch, fund_codes)
    if "fees" in aspects:
        tasks["fees"] = _batched(client.fee_rates_batch, fund_codes)
    if "discounts" in aspects:
        tasks["discounts"] = _batched(client.discounts_batch, fund_codes)
    if "profile" in aspects:
        tasks["profile"] = _fetch_profile(client, fund_codes)

    gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
    fetched: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, dict[str, str]] = {}
    for aspect, val in zip(tasks, gathered):
        if isinstance(val, BaseException):
            # 各路自己兜住单只 / 单批的失败，走到这里说明整路都没跑起来
            failures[aspect] = {code: _err(val) for code in fund_codes}
            continue
        rows, failed = val
        fetched[aspect] = rows
        if failed:
            failures[aspect] = failed

    per_fund: dict[str, dict[str, Any]] = {c: {} for c in fund_codes}

    if "basic" in fetched:
        for code, rows in _group_by_fund(fetched["basic"]).items():
            if code in per_fund:
                per_fund[code]["basic"] = [_slim(r, _BASIC_KEEP) for r in rows]

    if "limits" in fetched:
        filtered = _filter_custtype(fetched["limits"], custtype)
        picked = _pick_buy_limits(filtered)
        for code, rows in _group_by_fund(picked).items():
            if code in per_fund:
                per_fund[code]["limits"] = [_slim(r, _LIMIT_KEEP) for r in rows]

    if "fees" in fetched:
        filtered = _filter_custtype(fetched["fees"], custtype)
        for code, rows in _group_by_fund(filtered).items():
            if code in per_fund:
                per_fund[code]["fees"] = [_slim(r, _FEE_KEEP) for r in rows]

    if "discounts" in fetched:
        filtered = _filter_custtype(fetched["discounts"], custtype)
        for code, rows in _group_by_fund(filtered).items():
            if code in per_fund:
                per_fund[code]["discounts"] = [
                    _slim(r, _DISCOUNT_KEEP) for r in enrich_discount_rows(rows)
                ]

    if "profile" in fetched:
        for code, rows in _group_by_fund(fetched["profile"]).items():
            if code in per_fund and rows:
                per_fund[code]["profile"] = _slim(rows[0], _PROFILE_KEEP)

    for aspect in aspects:
        for code in failures.get(aspect, {}):
            if code in per_fund:
                per_fund[code].setdefault("failed", []).append(aspect)

    return {"custtype": custtype, "funds": per_fund}, failures


def _err(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


async def _fetch_profile(
    client: ProductCenterClient, codes: list[str]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """每只基金取 archive（成立日）+ performance（近一年涨幅/排名），合成一行。

    两个接口都按单只查，这里对每只并发发起；任一失败不影响另一只，
    也不影响 basic/limits/fees/discounts 那几路。失败的那一半记进 failures 并写明是哪一半；
    两半都失败时这只不出行。
    """
    failures: dict[str, str] = {}

    async def one(code: str) -> dict[str, Any] | None:
        arc, perf = await asyncio.gather(
            client.fund_archive(code),
            client.performance(code),
            return_exceptions=True,
        )
        row: dict[str, Any] = {"fund_code": code}
        problems: list[str] = []
        if isinstance(arc, BaseException):
            problems.append(f"档案接口（成立日）调用失败: {_err(arc)}")
        elif isinstance(arc, dict):
            row["establish_date"] = arc.get("establish_date")
        if isinstance(perf, BaseException):
            problems.append(f"业绩接口（近一年收益、同类排名）调用失败: {_err(perf)}")
        elif isinstance(perf, dict):
            for k in ("thisyear_rose", "thisyear_ranking", "year_rose", "year_ranking"):
                row[k] = perf.get(k)
            row["perf_workday"] = perf.get("workday")
            for per in perf.get("periods") or []:
                if str(per.get("rose_date_type")) == _PERIOD_YEAR:
                    row["year_total_fund_num"] = per.get("total_fund_num")
                    break
        if problems:
            failures[code] = "；".join(problems)
        if isinstance(arc, BaseException) and isinstance(perf, BaseException):
            return None
        return row

    sem = asyncio.Semaphore(CONCURRENCY)

    async def bounded(code: str) -> dict[str, Any] | None:
        async with sem:
            return await one(code)

    rows = [r for r in await asyncio.gather(*(bounded(c) for c in codes)) if r is not None]
    return rows, failures


async def _fetch_basic(
    client: ProductCenterClient, codes: list[str], custtype: str
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """detail 接口只能按只查，这里并发发起——原来是 for 串行，30 只就是 30 个往返串成一条。

    单只失败记进 failures。以前是塞一行带 error 的占位，结果被 _slim 剥掉 error、
    补上空状态字段，看起来就是「接口调通、状态全空」。
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    failures: dict[str, str] = {}

    async def one(code: str) -> list[dict[str, Any]]:
        async with sem:
            try:
                data = await client.fund_detail(code, custtype=custtype)
                return _expand_fund_detail_rows(data, custtype=custtype)
            except Exception as exc:  # noqa: BLE001
                logger.debug("multi_fund_basic_failed", code=code, error=str(exc))
                failures[code] = _err(exc)
                return []

    out: list[dict[str, Any]] = []
    for rows in await asyncio.gather(*(one(c) for c in codes)):
        out.extend(rows)
    return out, failures


async def _batched(fetch: Any, codes: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """批量接口按 BATCH_CHUNK 分片并发，避免 codes= 串过长。

    某一片失败只记这一片的代码，其余分片照常返回（以前一片失败整项作废，
    还被当成「接口调用成功但未返回记录」）。
    """
    chunks = [codes[i:i + BATCH_CHUNK] for i in range(0, len(codes), BATCH_CHUNK)]
    gathered = await asyncio.gather(*(fetch(c) for c in chunks), return_exceptions=True)
    out: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for chunk, part in zip(chunks, gathered):
        if isinstance(part, BaseException):
            failures.update({code: _err(part) for code in chunk})
            continue
        out.extend(part or [])
    return out, failures
