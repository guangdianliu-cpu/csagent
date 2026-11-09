"""SQLite 版 QueryBackend：把校验通过的 QuerySpec 翻成参数化 SQL。

服务 fund-research-db，覆盖 19 张投研核心与扩展数据表。
`select` 不经过 SemanticQueryExecutor 校验（引擎只管 filters/order_by），这里对照
真实表结构（PRAGMA table_info）做白名单校验——既防注入，也保证 select 出的列名
一定是当前 SQLite 里真实存在的列。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from csagent.tools.semantic.models import QuerySpec, SemanticQueryOutcome

_OP_SQL = {
    "eq": "=",
    # 「不等于」按直觉把空值行也算进来；SQL 的 != 会把 NULL 一起筛掉
    "ne": "IS NOT",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

KNOWN_TABLES = {
    "active_bond_funds",
    "active_equity_funds",
    "active_fixed_income_plus_funds",
    "qdii_equity_funds",
    "qdii_bond_funds",
    "fund_managers_equity",
    "fund_managers_fixed_income",
    "fund_trading_ability",
    "fund_companies",
    "convertible_bond_funds",
    "quant_equity_funds",
    "attribution_equity",
    "attribution_fixed_income",
    "industry_rotation_equity",
    "industry_rotation_fixed_income",
    "industry_allocation_sw",
    "industry_allocation_zx",
    "manager_industry_capabilities",
    "private_funds",
}

# 19 张表的主键、名称及描述列映射 (id_col, name_col, alt_name_col, desc_col)
TABLE_IDENTITY: dict[str, tuple[str, str, str | None, str | None]] = {
    "active_bond_funds": ("基金代码", "基金简称", None, "一句话基金画像"),
    "active_equity_funds": ("基金代码", "基金简称", None, "一句话基金画像"),
    "active_fixed_income_plus_funds": ("基金代码", "基金简称", None, "一句话基金画像"),
    "qdii_equity_funds": ("基金代码", "基金简称", None, "利得三级分类"),
    "qdii_bond_funds": ("基金代码", "基金简称", None, "利得三级分类"),
    "fund_managers_equity": ("基金经理", "基金经理", "基金公司", "偏好主题"),
    "fund_managers_fixed_income": ("基金经理", "基金经理", "基金公司", "偏好风格"),
    "fund_trading_ability": ("基金代码", "基金简称", None, "利得三级分类"),
    "fund_companies": ("基金管理人", "基金管理人简称", "基金管理人", None),
    "convertible_bond_funds": ("基金代码", "基金简称", None, "利得三级分类"),
    "quant_equity_funds": ("基金代码", "基金名称", None, "利得量化策略一级分类"),
    "attribution_equity": ("基金代码", "基金简称", None, "利得三级分类"),
    "attribution_fixed_income": ("基金代码", "基金简称", None, "利得三级分类"),
    "industry_rotation_equity": ("基金代码", "基金简称", None, "利得三级分类"),
    "industry_rotation_fixed_income": ("基金代码", "基金简称", None, "利得三级分类"),
    "industry_allocation_sw": ("基金代码", "基金名称", None, "基金类型"),
    "industry_allocation_zx": ("基金代码", "基金名称", None, "基金类型"),
    "manager_industry_capabilities": ("基金代码", "基金简称", None, "利得三级分类"),
    "private_funds": ("产品代码", "产品简称", "产品名称", "核心策略"),
}

# entity(=表名) → list 查询未指定 select 时的默认列
_DEFAULT_SELECT: dict[str, list[str]] = {
    "active_bond_funds": [
        "基金代码", "基金简称", "基金公司", "基金经理", "利得二级分类", "利得三级分类",
        "基金规模(合计)(亿元)", "基金规模评价", "申购状态",
        "近1年收益(%)", "近1年最大回撤(%)", "综合评分", "一句话基金画像",
    ],
    "active_equity_funds": [
        "基金代码", "基金简称", "基金公司", "基金经理", "利得二级分类", "利得三级分类",
        "基金规模(合计)(亿元)", "基金规模评价", "申购状态",
        "近1年收益(%)", "近1年最大回撤(%)", "综合评分", "一句话基金画像",
    ],
    "active_fixed_income_plus_funds": [
        "基金代码", "基金简称", "基金公司", "基金经理", "利得二级分类", "利得三级分类", "风险特征",
        "基金规模(合计)(亿元)", "申购状态",
        "近1年收益(%)", "近1年最大回撤(%)", "综合评分", "一句话基金画像",
    ],
    "qdii_equity_funds": [
        "基金代码", "基金简称", "Wind三级分类", "利得一级分类", "利得二级分类", "利得三级分类",
        "基金公司", "基金经理", "基金规模(亿元)", "基金规模评价", "近1年收益(%)", "综合评分",
    ],
    "qdii_bond_funds": [
        "基金代码", "基金简称", "Wind三级分类", "利得一级分类", "利得二级分类", "利得三级分类",
        "基金公司", "基金经理", "其中：最新美国国债占比%", "最新信用债占比%", "额度", "近1年收益(%)",
    ],
    "fund_managers_equity": [
        "基金经理", "管理年限(年)", "在管基金数", "基金公司", "职业生涯年化收益率(%)", "在管基金规模合计(亿元)",
        "主动权益代表基金代码", "代表基金简称", "偏好主题", "偏好行业", "擅长主题", "擅长行业", "近三年评分_综合评分",
    ],
    "fund_managers_fixed_income": [
        "基金经理", "管理年限(年)", "在管基金数", "基金公司", "职业生涯年化收益率(%)", "在管基金规模合计(亿元)",
        "主动固收+代表基金代码", "代表基金简称", "MRQ久期", "偏好风格", "擅长行业", "近三年评分_综合评分",
    ],
    "fund_trading_ability": [
        "基金代码", "基金简称", "利得二级分类", "利得三级分类", "基金经理", "最新股票仓位(%)",
        "近三年交易能力评分", "近三年平均换手率(%)", "2026年交易能力评分", "2026年换手率(%)",
    ],
    "fund_companies": [
        "基金管理人", "基金管理人简称", "总分", "非货管理规模(亿元)", "非货币基金数量(只)[产品口径]",
        "成立年限(年)", "是否具备社保基金管理人资格", "是否具备养老金管理资格", "是否具备年金管理资格",
        "权益投研业绩(%)", "固收投研业绩(%)",
    ],
    "convertible_bond_funds": [
        "基金代码", "基金简称", "基金公司", "基金经理", "Wind二级分类", "利得二级分类", "利得三级分类",
        "基金规模(合计)(亿元)", "股票持仓占比(%)", "可转债持仓占比(%)", "近1年收益(%)", "综合评分",
    ],
    "quant_equity_funds": [
        "基金代码", "基金名称", "基金成立日", "基金公司", "基金经理", "Wind分类", "利得一级分类",
        "利得量化策略一级分类", "利得量化策略二级分类", "基金评分", "今年以来收益(%)", "申购状态",
    ],
    "attribution_equity": [
        "基金代码", "基金简称", "利得二级分类", "利得三级分类", "基金经理", "基金公司", "基金规模(亿元)",
        "业绩归因收益总分", "业绩归因波动总分", "业绩归因总分", "股票资产配置_收益", "行业配置超额_收益",
        "个股选择超额_收益", "交易收益_收益",
    ],
    "attribution_fixed_income": [
        "基金代码", "基金简称", "利得二级分类", "利得三级分类", "基金经理", "基金公司", "基金规模(亿元)",
        "业绩归因收益总分", "业绩归因波动总分", "业绩归因总分", "纯债资产配置_收益", "可转债资产配置_收益",
        "股票资产配置_收益",
    ],
    "industry_rotation_equity": [
        "基金代码", "基金简称", "利得二级分类", "利得三级分类", "基金公司", "基金经理", "基金规模(亿元)",
        "是否主动择时", "是否主题轮动", "是否行业轮动", "近三年行业轮动能力评分", "基金评价_近三年行业配置能力评分",
        "近三年收益率(%)", "近三年换手率风格",
    ],
    "industry_rotation_fixed_income": [
        "基金代码", "基金简称", "利得二级分类", "利得三级分类", "基金公司", "基金经理", "基金规模(亿元)",
        "是否主动择时", "是否主题轮动", "是否行业轮动", "近三年行业轮动能力评分", "基金评价_近三年行业配置能力评分",
        "近三年收益率(%)", "近三年换手率风格",
    ],
    "industry_allocation_sw": [
        "基金代码", "基金名称", "成立时间", "基金公司", "基金类型", "基金规模(合计)(亿元)", "基金经理",
        "利得二级分类", "利得三级分类", "近1年收益(%)", "近1年最大回撤(%)", "申购状态",
    ],
    "industry_allocation_zx": [
        "基金代码", "基金名称", "成立时间", "基金公司", "基金类型", "基金规模(合计)(亿元)", "基金经理",
        "利得二级分类", "利得三级分类", "近1年收益(%)", "近1年最大回撤(%)", "申购状态",
    ],
    "manager_industry_capabilities": [
        "基金代码", "基金简称", "基金公司简称", "基金经理", "利得一级分类", "利得二级分类", "利得三级分类",
        "股票仓位(%)", "科技TMT_持仓比例_平均值(%)", "新能源_持仓比例_平均值(%)", "消费服务_持仓比例_平均值(%)",
        "医药生物_持仓比例_平均值(%)", "高端制造_持仓比例_平均值(%)",
    ],
    "private_funds": [
        "产品代码", "产品名称", "产品简称", "一级分类", "二级分类", "三级策略", "管理人名称", "核心策略",
        "管理规模", "今年以来收益", "近一年最大回撤", "成立以来年化收益率", "复权净值", "基金状态",
    ],
}


# 数值列取值不超过这么多种时（评分档位、行业数这类），values 顺带给出每个取值的条数
_FEW_DISTINCT = 20

# 名称近似匹配只给按代码定位的基金 / 产品表；人名、公司名做子序列匹配容易张冠李戴
_FUZZY_ID_COLS = frozenset({"基金代码", "产品代码"})
_FUZZY_MIN_CHARS = 4
_FUZZY_MAX_CANDIDATES = 5
# 没对上时，相似度不低于这个的名字放进 similar 供核对（「兰筹精选」对「蓝筹精选」是 0.75；
# 只沾了公司名和一个字的，如「工银瑞信双利」对「工银红利」是 0.6，不给）
_FUZZY_SIMILAR_CUTOFF = 0.7
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
# 份额后缀：A / C / E / H / Y……，可带「类」「份额」；前一个字也是字母的不算（ETF、LOF）
_SHARE_CLASS_RE = re.compile(r"(?<![A-Z])([A-Z])(?:类|份额)?$")
# 全称里常有、库里简称多半省掉的类型词，比较前两边都去掉
_TYPE_WORDS_RE = re.compile(r"证券投资基金|发起式|债券型|混合型|股票型|基金|债券|混合|股票")
# 「工银瑞信基金」「东方红资产管理」去掉尾巴，剩下的是用户嘴里的公司名
_COMPANY_SUFFIX_RE = re.compile(r"(?:证券资管|资产管理|资管|资产|基金|证券)$")


def _fold(text: str) -> str:
    """比较用的写法：全角转半角、去括号注记与空白、字母大写。"""
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", _PAREN_RE.sub("", text)).upper()


def _name_core(name: str) -> tuple[str, str]:
    """名字主体与份额：「工银瑞信双利债券A」→ (「工银瑞信双利」, 「A」)；没写份额时第二项为空。"""
    text = _fold(name)
    share_class = ""
    m = _SHARE_CLASS_RE.search(text)
    if m and m.start() >= 2:
        text, share_class = text[: m.start()], m.group(1)
    return _TYPE_WORDS_RE.sub("", text), share_class


def _contains(short: str, long: str) -> bool:
    """short 的字按顺序都出现在 long 里（「双利」⊂「工银双利」）；两个字以内要求连着出现。"""
    if len(short) <= 2:
        return short in long
    chars = iter(long)
    return all(ch in chars for ch in short)


def _name_match(key: str, core: str, stem: str) -> tuple[bool, float]:
    """(对没对上, 字形相似度)。

    stem 为空：key 与 core 都是完整名字主体，要求开头两个字相同（简称都以公司简称打头）。
    stem 非空：两边已是同一家公司，key 是去掉公司名后的主体；库里简称的公司部分多是两个字
    （工银、兴全、大摩），也有写全的（工银瑞信稳润），两种去法都试。
    """
    if not stem:
        if len(core) < _FUZZY_MIN_CHARS:
            return False, 0.0
        hit = key in core or (core[:2] == key[:2] and (_contains(key, core) or _contains(core, key)))
        return hit, difflib.SequenceMatcher(None, key, core).ratio()
    lcp = len(os.path.commonprefix([core, stem]))
    rests = {r for r in (core[2:], core[lcp:] if lcp > 2 else "") if len(r) >= 2}
    if not rests:
        return False, 0.0
    hit = _contains(key, core[2:]) or any(_contains(r, key) for r in rests)
    return hit, max(difflib.SequenceMatcher(None, key, r).ratio() for r in rests)


def _brief(rows: list[sqlite3.Row], id_col: str, name_col: str, desc_col: str | None) -> list[dict[str, Any]]:
    """候选列表：代码、名称，加一列描述帮着分辨。"""
    brief = []
    for r in rows:
        item = {id_col: r[id_col], name_col: r[name_col]}
        if desc_col and desc_col in r.keys():
            item[desc_col] = r[desc_col]
        brief.append(item)
    return brief


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _column_types(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    return {row[1]: str(row[2] or "").upper() for row in conn.execute(f'PRAGMA table_info("{table}")')}


class SqliteBackend:
    """fund-research-db 专用 QueryBackend 实现。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    async def execute(self, spec: QuerySpec) -> SemanticQueryOutcome:
        table = spec.entity
        if table not in KNOWN_TABLES:
            return SemanticQueryOutcome(entity=table, text=f"未知实体/表名: {table}")

        conn = self._connect()
        try:
            real_cols = table_columns(conn, table)
            real_col_set = set(real_cols)

            select_cols = spec.select or _DEFAULT_SELECT.get(table, real_cols[:12])
            bad = [c for c in select_cols if c not in real_col_set]
            if bad:
                return SemanticQueryOutcome(
                    entity=table,
                    text=f"select 里有不存在的列: {bad}；用 query.py --action fields 查真实列名",
                )

            where_sql, params = _build_where(spec, real_col_set)
            if isinstance(where_sql, str) and where_sql.startswith("__ERROR__"):
                return SemanticQueryOutcome(entity=table, text=where_sql.removeprefix("__ERROR__"))

            order_sql = ""
            if spec.order_by is not None:
                if spec.order_by.field not in real_col_set:
                    return SemanticQueryOutcome(entity=table, text=f"排序字段不存在: {spec.order_by.field}")
                direction = "DESC" if spec.order_by.direction == "desc" else "ASC"
                order_sql = f' ORDER BY "{spec.order_by.field}" {direction}'

            limit = spec.limit or 20
            offset = max(0, spec.offset or 0)
            page_order_sql = order_sql or " ORDER BY rowid ASC"
            cols_sql = ", ".join(f'"{c}"' for c in select_cols)
            sql = f'SELECT {cols_sql} FROM "{table}"{where_sql}{page_order_sql} LIMIT ? OFFSET ?'
            rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset)).fetchall()]

            total_sql = f'SELECT COUNT(*) FROM "{table}"{where_sql}'
            total = conn.execute(total_sql, params).fetchone()[0]

            next_offset = offset + len(rows)
            has_more = next_offset < total
            payload = {
                "entity": table,
                "matched": total,
                "returned": len(rows),
                "offset": offset,
                "truncated": has_more,
                "next_offset": next_offset if has_more else None,
                "hint": (
                    f"还有 {total - next_offset} 条未返回；"
                    f'重复本次查询并加 "offset": {next_offset} 取下一页'
                )
                if has_more
                else None,
                "rows": rows,
            }
            return SemanticQueryOutcome(
                entity=table,
                text=json.dumps(payload, ensure_ascii=False, default=str),
                rows=rows,
                row_limit=limit,
            )
        finally:
            conn.close()

    def get_fund(self, table: str, keyword: str) -> str:
        """主键/代码优先精确匹配，退化为名称模糊匹配。"""
        conn = self._connect()
        try:
            id_col, name_col, alt_name_col, desc_col = TABLE_IDENTITY.get(
                table, ("基金代码", "基金简称", None, None)
            )

            # 1. 主键精确匹配
            code_candidates = [keyword]
            bare = keyword.strip().upper().removesuffix(".OF")
            if bare.isdigit() and id_col == "基金代码":
                with_suffix = f"{bare}.OF"
                if with_suffix != keyword:
                    code_candidates.append(with_suffix)

            for code in code_candidates:
                row = conn.execute(
                    f'SELECT * FROM "{table}" WHERE "{id_col}" = ?', (code,)
                ).fetchone()
                if row is not None:
                    return json.dumps(
                        {"entity": table, "match": "exact_code", "fund": dict(row)},
                        ensure_ascii=False, default=str,
                    )

            # 2. 名称模糊匹配
            name_clauses = [f'"{name_col}" LIKE ?']
            params = [f"%{keyword}%"]
            if alt_name_col and alt_name_col != name_col:
                name_clauses.append(f'"{alt_name_col}" LIKE ?')
                params.append(f"%{keyword}%")

            where_or = " OR ".join(name_clauses)
            rows = conn.execute(
                f'SELECT * FROM "{table}" WHERE ({where_or}) LIMIT 20', params
            ).fetchall()

            if not rows and id_col in _FUZZY_ID_COLS and not bare.isdigit():
                return self._fuzzy_get(conn, table, keyword, id_col, name_col, desc_col)
            if not rows:
                return json.dumps({"entity": table, "match": "none", "keyword": keyword}, ensure_ascii=False)
            if len(rows) == 1:
                return json.dumps(
                    {"entity": table, "match": "unique_name", "fund": dict(rows[0])},
                    ensure_ascii=False, default=str,
                )
            return json.dumps(
                {
                    "entity": table,
                    "match": "ambiguous",
                    "keyword": keyword,
                    "candidates": _brief(rows, id_col, name_col, desc_col),
                },
                ensure_ascii=False, default=str,
            )
        finally:
            conn.close()

    def _fuzzy_get(
        self,
        conn: sqlite3.Connection,
        table: str,
        keyword: str,
        id_col: str,
        name_col: str,
        desc_col: str | None,
    ) -> str:
        """名称近似匹配：「工银瑞信双利债券」→「工银双利A」。

        LIKE 只认连续子串，用户说的却常是全称：公司写全称（库里简称是「工银」「兴全」「大摩」）、
        带「债券」「混合」这类类型词、份额写法不一（2026-10-22 审计：一只基金为此多查了 4 个来回）。
        两边都去掉括号注记、份额与类型词；用户开头是本表某家公司的名字时，只在这家公司的基金里
        比去掉公司名后的主体。对上一只直接给档案并注明是近似匹配；对上多只给候选；
        都没对上时把名字相近的几只放进 similar，省得模型再拿关键字大范围 list。
        """
        key_core, key_class = _name_core(keyword)
        company_col = None
        if id_col == "基金代码":
            cols = set(table_columns(conn, table))
            company_col = next((c for c in ("基金公司", "基金公司简称") if c in cols), None)
        stem = ""
        if company_col:
            stems = {
                _COMPANY_SUFFIX_RE.sub("", _fold(str(v)))
                for (v,) in conn.execute(
                    f'SELECT DISTINCT "{company_col}" FROM "{table}" WHERE "{company_col}" IS NOT NULL'
                )
            }
            stem = max((s for s in stems if len(s) >= 2 and key_core.startswith(s)), key=len, default="")
        key = key_core[len(stem):]

        miss: dict[str, Any] = {
            "entity": table,
            "match": "none",
            "keyword": keyword,
            "hint": "名称近似匹配也没对上：按基金代码查最可靠；也可能不在这张表，换个 entity 再查",
        }
        if len(key) < (2 if stem else _FUZZY_MIN_CHARS):
            return json.dumps(miss, ensure_ascii=False)

        if stem:
            where, params = f'"{company_col}" LIKE ?', (f"{stem}%",)
        else:
            where, params = f'("{name_col}" LIKE ? OR "{name_col}" LIKE ?)', (f"{key[:2]}%", f"%{key}%")
        hits: list[tuple[bool, float, int]] = []
        near: list[tuple[bool, float, int]] = []
        for rowid, name in conn.execute(
            f'SELECT rowid, "{name_col}" FROM "{table}" WHERE "{name_col}" IS NOT NULL AND {where}', params
        ):
            core, share_class = _name_core(str(name))
            hit, ratio = _name_match(key, core, stem)
            entry = (bool(key_class) and share_class == key_class, ratio, rowid)
            if hit:
                hits.append(entry)
            elif ratio >= _FUZZY_SIMILAR_CUTOFF:
                near.append(entry)

        def fetch(entries: list[tuple[bool, float, int]]) -> list[sqlite3.Row]:
            entries = sorted(entries, reverse=True)[:_FUZZY_MAX_CANDIDATES]
            return [conn.execute(f'SELECT * FROM "{table}" WHERE rowid = ?', (e[2],)).fetchone() for e in entries]

        if not hits:
            if near:
                miss["similar"] = _brief(fetch(near), id_col, name_col, desc_col)
                miss["hint"] = (
                    "名称近似匹配也没对上；similar 是名字相近的几只，只供核对，不要直接当成用户问的那只——"
                    "拿不准就向用户确认全称或代码；也可能不在这张表，换个 entity 再查"
                )
            return json.dumps(miss, ensure_ascii=False, default=str)

        if key_class and any(h[0] for h in hits):
            hits = [h for h in hits if h[0]]  # 用户点名了份额，只留同份额的
        exact = [h for h in hits if h[1] == 1.0]
        if len(exact) == 1:
            hits = exact  # 主体一字不差的只有一只（「深证红利ETF联接」对上联接基金，不再列 ETF 本身）
        rows = fetch(hits)
        if len(hits) > 1:
            return json.dumps(
                {
                    "entity": table,
                    "match": "ambiguous",
                    "fuzzy": True,
                    "keyword": keyword,
                    "candidates": _brief(rows, id_col, name_col, desc_col),
                    "note": "名称近似匹配到多只（按相似度排），挑出用户说的那只用代码再查；拿不准就向用户确认",
                },
                ensure_ascii=False, default=str,
            )
        row = rows[0]
        note = f"名称近似匹配到「{row[name_col]}」，和用户说的写法不完全一致，作答时注明用的是这只"
        if key_class and not hits[0][0]:
            note += f"；用户问的是 {key_class} 类，库里这只基金只收了这一份额（各份额持仓、经理相同，费率与业绩略有差别）"
        return json.dumps(
            {
                "entity": table,
                "match": "fuzzy_name",
                "keyword": keyword,
                "matched_name": row[name_col],
                "note": note,
                "fund": dict(row),
            },
            ensure_ascii=False, default=str,
        )

    def fields(self, table: str) -> str:
        conn = self._connect()
        try:
            cols = table_columns(conn, table)
            return json.dumps({"entity": table, "columns": cols, "column_count": len(cols)}, ensure_ascii=False)
        finally:
            conn.close()

    def column_types(self, table: str) -> dict[str, str]:
        """列名 → 声明类型。build_cache 建表时全数值的列记 REAL，其余记 TEXT。"""
        conn = self._connect()
        try:
            return _column_types(conn, table)
        finally:
            conn.close()

    def field_values(self, table: str, field: str, spec: QuerySpec, limit: int) -> str:
        """某个字段在（可带 filters 的）结果里有哪些取值。

        文本列给取值与条数，按条数从多到少；数值列给分位数分布，取值不超过
        ``_FEW_DISTINCT`` 种时再按取值列出条数。
        """
        conn = self._connect()
        try:
            types = _column_types(conn, table)
            if field not in types:
                return json.dumps({"error": f"字段不存在: {field}"}, ensure_ascii=False)
            where_sql, params = _build_where(spec, set(types))
            if where_sql.startswith("__ERROR__"):
                return json.dumps({"error": where_sql.removeprefix("__ERROR__")}, ensure_ascii=False)

            col = f'"{field}"'
            not_null = _and_where(where_sql, f"{col} IS NOT NULL")
            numeric = types[field] == "REAL"
            payload: dict[str, Any] = {
                "entity": table,
                "field": field,
                "type": "number" if numeric else "text",
                "matched": conn.execute(f'SELECT COUNT(*) FROM "{table}"{where_sql}', params).fetchone()[0],
                "nulls": conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"{_and_where(where_sql, f"{col} IS NULL")}', params
                ).fetchone()[0],
                "distinct": conn.execute(
                    f'SELECT COUNT(DISTINCT {col}) FROM "{table}"{where_sql}', params
                ).fetchone()[0],
            }

            if numeric:
                nums = [r[0] for r in conn.execute(f'SELECT {col} FROM "{table}"{not_null} ORDER BY {col}', params)]
                if nums:
                    payload.update({
                        "min": _num(nums[0]),
                        "p25": _num(_quantile(nums, 0.25)),
                        "median": _num(_quantile(nums, 0.5)),
                        "p75": _num(_quantile(nums, 0.75)),
                        "max": _num(nums[-1]),
                        "avg": _num(sum(nums) / len(nums)),
                    })
                if payload["distinct"] <= _FEW_DISTINCT:
                    rows = conn.execute(
                        f'SELECT {col}, COUNT(*) FROM "{table}"{not_null} GROUP BY {col} ORDER BY {col}', params
                    ).fetchall()
                    payload["values"] = [{"value": v, "count": n} for v, n in rows]
            else:
                rows = conn.execute(
                    f'SELECT {col}, COUNT(*) AS n FROM "{table}"{not_null} GROUP BY {col} ORDER BY n DESC, {col} LIMIT ?',
                    (*params, limit),
                ).fetchall()
                payload["returned"] = len(rows)
                payload["truncated"] = payload["distinct"] > len(rows)
                payload["values"] = [{"value": v, "count": n} for v, n in rows]
            return json.dumps(payload, ensure_ascii=False, default=str)
        finally:
            conn.close()


def _build_where(spec: QuerySpec, real_col_set: set[str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for flt in spec.filters:
        if flt.field not in real_col_set:
            return f"__ERROR__筛选字段不存在: {flt.field}", []
        col = f'"{flt.field}"'
        if flt.op == "in":
            values = flt.value if isinstance(flt.value, list) else [flt.value]
            if not values:
                return f"__ERROR__字段 {flt.field} 的 in 操作值不能为空列表", []
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f"{col} IN ({placeholders})")
            params.extend(values)
        elif flt.op == "like":
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{flt.value}%")
        elif flt.op in _OP_SQL:
            clauses.append(f"{col} {_OP_SQL[flt.op]} ?")
            params.append(flt.value)
        else:
            return f"__ERROR__不支持的操作符: {flt.op}", []
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def _and_where(where_sql: str, cond: str) -> str:
    return f"{where_sql} AND {cond}" if where_sql else f" WHERE {cond}"


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认口径一致）；调用方保证非空且已升序。"""
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _num(value: float) -> float:
    return round(float(value), 4)
