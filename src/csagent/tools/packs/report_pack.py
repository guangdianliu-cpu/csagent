"""报告工具包：publish_report / update_report / read_report。

docs/对话页升级方案.md §3.3。模型交结构化积木，服务端校验、按会话落版本、入出站队列；
网页端在本轮文字结束后收到 ``report`` 事件，渲染成卡片。
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from csagent.channels.outbound_reports import OutboundReport, enqueue_outbound_report
from csagent.memory.artifact_store import register_artifact
from csagent.react.models import CustomerScope
from csagent.reports import export, store
from csagent.reports.model import ReportContent, readable_errors
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)

_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "一块内容。type=markdown：text（可含 ## 小标题、列表）；"
        "type=table：title（表名，自动编成「表 n」）、columns、rows（二维数组，每行格数=列数）、"
        "signed_columns（这些列的正负数按涨红跌绿着色，如收益率列）、source（资料来源）；"
        "对比类表格加记分卡：row_best（行是指标、列是对象时，每行一个 max/min/null，与 rows 等长）"
        "或 col_best（行是对象时，{列名: max|min}），最优格自动高亮；回撤写成负数时最优是 max；"
        "type=chart：title（图名，自动编成「图 n」）、chart=line|bar|pie|rank|scatter、unit（数值单位，如 %）、source；"
        "line/bar/pie/rank 用 x（标签）+ series=[{name, data}]（data 与 x 等长；pie、rank 只能 1 个系列）；"
        "rank 是横向排名条，自动从大到小排，适合排序 / Top N；"
        "scatter 用 points=[{name, x, y}] + x_label + y_label，适合风险-收益这类两指标对比；"
        "percentile 是分位条：x=指标名，series 1 个、data=同类前 x%（0–100，越小越好），display=每项原始值（如 +8.2%），"
        "x_label=同类口径（如「同类 1,203 只」）；"
        "contrib 是贡献条：x=资产 / 行业，series 2 个（第 1 个占比、第 2 个贡献），看「占多少、贡献多少」；"
        "bar 的类目多或标签长会自动改横向；"
        "type=entities：对象卡，items=[{name, kind（类型）, tags（≤4 个短标签）, note（一句话画像）}]，"
        "metrics=[{label, values（与 items 等长）, best: max|min|null}]；1 个对象=画像卡，2 个=对决卡"
        "（逐项比、自动算各胜几项），3–5 个=并排卡；"
        "type=fit：适合 / 不适合两栏，good=[…]、bad=[…]（每条 ≤30 字），可改 good_label / bad_label；"
        "type=conclusion：text（本章小结，单独高亮）。"
    ),
    "properties": {
        "type": {"type": "string", "enum": ["markdown", "table", "chart", "conclusion", "entities", "fit"]},
        "text": {"type": "string"},
        "title": {"type": "string"},
        "source": {"type": "string"},
        "unit": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "array"}},
        "signed_columns": {"type": "array", "items": {"type": "string"}},
        "row_best": {"type": "array", "items": {"type": ["string", "null"], "enum": ["max", "min", None]}},
        "col_best": {"type": "object", "additionalProperties": {"type": "string", "enum": ["max", "min"]}},
        "chart": {"type": "string", "enum": ["line", "bar", "pie", "rank", "scatter", "percentile", "contrib"]},
        "display": {"type": "array", "items": {"type": "string"}},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "values": {"type": "array"},
                    "best": {"type": ["string", "null"], "enum": ["max", "min", None]},
                },
                "required": ["label", "values"],
            },
        },
        "good": {"type": "array", "items": {"type": "string"}},
        "bad": {"type": "array", "items": {"type": "string"}},
        "good_label": {"type": "string"},
        "bad_label": {"type": "string"},
        "x_label": {"type": "string"},
        "y_label": {"type": "string"},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["name", "x", "y"],
            },
        },
        "x": {"type": "array", "items": {"type": "string"}},
        "series": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "data": {"type": "array", "items": {"type": ["number", "null"]}},
                },
                "required": ["name", "data"],
            },
        },
    },
    "required": ["type"],
}

_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "一个章节；标题不要带序号，「一、二、三」自动编",
    "properties": {
        "title": {"type": "string", "description": "章节标题，≤40 字"},
        "lead": {"type": "string", "description": "本节结论一句话（≤40 字），加粗显示在标题下；写了就不要再加 conclusion"},
        "blocks": {"type": "array", "items": _BLOCK_SCHEMA, "description": "本章内容块，按顺序"},
    },
    "required": ["title", "blocks"],
}

_CONTENT_PROPS: dict[str, Any] = {
    "title": {"type": "string", "description": "报告标题，≤60 字"},
    "summary": {"type": "string", "description": "给卡片和转发用的一句话，≤60 字；报告头有 verdict 时不显示"},
    "as_of": {"type": "string", "description": "数据截至日，如 2026-10-28；不知道就不填"},
    "verdict": {"type": "string", "description": "一句话结论（≤30 字），报告头最醒目处，如「稳健型首选乙，甲次选，丙不建议」"},
    "kpis": {
        "type": "array",
        "description": "指标卡 2–4 个：最能支撑结论的数字。value 写成显示用的文字（如 -6%、1.2、85 亿）",
        "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "≤16 字，如「最大回撤 · 乙」"},
                "value": {"type": "string", "description": "≤16 字"},
                "note": {"type": "string", "description": "一行注释 ≤12 字，如「三只中最小」"},
                "tone": {"type": "string", "enum": ["auto", "neutral", "up", "down"], "description": "auto 按正负涨红跌绿；不是收益类的数用 neutral"},
            },
            "required": ["label", "value"],
        },
    },
    "key_points": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "≤40 字的一句大白话，最多一两个数字"},
                "tone": {"type": "string", "enum": ["plus", "tradeoff", "risk", "action", "info"], "description": "标记：plus 优势 / tradeoff 权衡 / risk 风险 / action 建议 / info 不标"},
            },
            "required": ["text"],
        },
        "description": "核心观点 2–5 条，放在报告最前面",
    },
    "sections": {"type": "array", "items": _SECTION_SCHEMA, "description": "正文章节，1–12 章"},
    "risk_note": {"type": "string", "description": "本报告特有的风险或口径提示；通用免责声明会自动加，不用写"},
    "sources": {"type": "array", "items": {"type": "string"}, "description": "数据来源，≤10 条"},
}
# 旧格式（平铺 blocks）只读不写：新报告一律按章节交
_REQUIRED = ["title", "summary", "key_points", "sections"]

_AFTER_PUBLISH = (
    "本轮文字回复结束后，用户会看到一张报告卡片，点开是完整报告。"
    "正文只写 1 句结论，不要重复报告里的表格和段落。"
)


def _strip_nulls(blocks: list) -> list:
    """模型常给每块都带上全部字段（不用的填 null / 空串），按类型校验前去掉。"""
    out = []
    for b in blocks:
        if isinstance(b, dict):
            b = {k: v for k, v in b.items() if k == "type" or (v is not None and v != "")}
            # 对象卡的 items 也会带 kind: null 之类
            if isinstance(b.get("items"), list):
                b["items"] = _strip_fields(b["items"])
        out.append(b)
    return out


def _strip_fields(items: list) -> list:
    return [
        {k: v for k, v in it.items() if v is not None and v != ""} if isinstance(it, dict) else it
        for it in items
    ]


def _clean(raw: dict) -> dict:
    if isinstance(raw.get("kpis"), list):
        raw["kpis"] = _strip_fields(raw["kpis"])
    if isinstance(raw.get("key_points"), list):
        raw["key_points"] = _strip_fields(raw["key_points"])
    sections = raw.get("sections")
    if isinstance(sections, list):
        raw["sections"] = [
            {**sec, "blocks": _strip_nulls(sec.get("blocks") or [])} if isinstance(sec, dict) else sec
            for sec in sections
        ]
    if isinstance(raw.get("blocks"), list):
        raw["blocks"] = _strip_nulls(raw["blocks"])
    return raw


# 发布前的篇幅检查：比模型字段的存储上限紧，只管新写的内容（旧报告照常能读）。
# 超了就退回让模型压短——报告头是给人 5 秒看完的，写长了就没人看。
_BREVITY = {"verdict": 40, "key_point": 60, "kpi_note": 16, "lead": 60, "paragraph": 160, "section": 500}


def _paragraphs(md: str) -> list[str]:
    """正文段落（不含表格、标题行）；列表一项算一段。"""
    out = []
    for chunk in md.replace("\r\n", "\n").split("\n\n"):
        lines = [x.strip() for x in chunk.split("\n") if x.strip() and not x.strip().startswith(("|", "#", "```"))]
        if lines and all(x[:1] in "-*+" or x[:2].rstrip(".)").isdigit() for x in lines):
            out.extend(lines)
        elif lines:
            out.append("".join(lines))
    return [x.replace("**", "") for x in out]


def _brevity_problems(content: ReportContent) -> list[str]:
    out = []
    if len(content.verdict) > _BREVITY["verdict"]:
        out.append(f"verdict {len(content.verdict)} 字，压到 30 字左右")
    for i, p in enumerate(content.points(), 1):
        if len(p.text) > _BREVITY["key_point"]:
            out.append(f"key_points 第 {i} 条 {len(p.text)} 字，压到 40 字左右")
    for k in content.kpis:
        if len(k.note) > _BREVITY["kpi_note"]:
            out.append(f"指标卡「{k.label}」的 note {len(k.note)} 字，压到 12 字以内")
    for sec in content.sections:
        if len(sec.lead) > _BREVITY["lead"]:
            out.append(f"章节「{sec.title}」的 lead {len(sec.lead)} 字，压到 40 字左右")
        paras = [x for b in sec.blocks if b.type == "markdown" for x in _paragraphs(b.text)]
        long = [x for x in paras if len(x) > _BREVITY["paragraph"]]
        if long:
            out.append(f"章节「{sec.title}」有 {len(long)} 段超过 {_BREVITY['paragraph']} 字（如「{long[0][:16]}…」），拆成短段或删掉重复的话")
        total = sum(len(x) for x in paras)
        if total > _BREVITY["section"]:
            out.append(f"章节「{sec.title}」正文共 {total} 字，压到 200 字左右，细节放进表格或对象卡")
    return out


def _brevity_error(content: ReportContent, verb: str) -> str:
    problems = _brevity_problems(content)
    if not problems:
        return ""
    return f"错误: 有几处写得太长，未{verb}。{'；'.join(problems)}。用一句大白话重写这几处后再提交。"


def _publish(scope: CustomerScope, content: ReportContent, report_id: str = "") -> str:
    report, path = store.save(scope.session_id, content, report_id=report_id)
    enqueue_outbound_report(
        scope.session_id,
        OutboundReport(
            report_id=report.report_id,
            version=report.version,
            title=report.title,
            summary=report.summary,
            has_tables=export.has_tables(report),
        ),
    )
    register_artifact(
        session_id=scope.session_id,
        kind="report",
        path=store.relative_path(path),
        filename=f"{report.title} v{report.version}",
    )
    logger.info(
        "report_published",
        session_id=scope.session_id,
        report_id=report.report_id,
        version=report.version,
        sections=len(report.sections),
        blocks=len(report.all_blocks()),
    )
    verb = "已发布" if report.version == 1 else "已更新"
    return f"{verb}报告 {report.report_id} v{report.version}「{report.title}」。{_AFTER_PUBLISH}"


class PublishReportTool(BaseTool):
    name = "publish_report"
    description = (
        "把一份较长的分析整理成报告卡片发给用户（对比、多章节分析、≥10 行的表）。"
        "单个数字、口径解释、一两句话能答完的不要用。"
        "版式固定：报告头 → 核心观点 → 目录 → 编号章节 → 风险提示 → 数据来源，你只填内容。"
        "参数 title / summary / key_points / sections（每章 title + blocks）/ as_of / risk_note / sources；"
        "校验不通过会返回错误，按提示改了再调。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": _CONTENT_PROPS,
            "required": _REQUIRED,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        raw = _clean({k: arguments.get(k) for k in _CONTENT_PROPS if arguments.get(k) is not None})
        missing = [k for k in _REQUIRED if not raw.get(k)]
        if missing:
            return f"错误: 缺少 {'、'.join(missing)}，未发布。"
        try:
            content = ReportContent.model_validate(raw)
        except ValidationError as exc:
            return f"错误: 报告格式不对，未发布。{readable_errors(exc)}"
        if err := _brevity_error(content, "发布"):
            return err
        return _publish(scope, content)


class UpdateReportTool(BaseTool):
    name = "update_report"
    description = (
        "在已有报告上出新版本（用户要求改报告时用，不要另发一份）。"
        "report_id 不填就是本会话最近一份。只传要改的字段，没传的沿用上一版；"
        "要改其中一部分内容，先 read_report 拿到完整 blocks，改好后整组传回。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"report_id": {"type": "string"}, **_CONTENT_PROPS},
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        rid = str(arguments.get("report_id") or "").strip() or store.latest_id(scope.session_id)
        if not rid:
            return "错误: 本会话还没有报告，请用 publish_report 新建。"
        current = store.load(scope.session_id, rid)
        if current is None:
            return f"错误: 本会话没有报告 {rid}。"
        merged = current.model_dump(include=set(_CONTENT_PROPS) | {"blocks"})
        for key in _CONTENT_PROPS:
            if arguments.get(key) is not None:
                merged[key] = arguments[key]
        # 旧报告改版时换成章节格式：给了 sections 就丢掉平铺的 blocks
        if merged.get("sections"):
            merged["blocks"] = []
        merged = _clean(merged)
        try:
            content = ReportContent.model_validate(merged)
        except ValidationError as exc:
            return f"错误: 报告格式不对，未更新。{readable_errors(exc)}"
        if err := _brevity_error(content, "更新"):
            return err
        return _publish(scope, content, report_id=rid)


class ReadReportTool(BaseTool):
    name = "read_report"
    description = "读取本会话一份报告的完整内容（JSON）。report_id 不填读最近一份；version 不填读最新版。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
                "version": {"type": "integer"},
            },
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        rid = str(arguments.get("report_id") or "").strip() or store.latest_id(scope.session_id)
        if not rid:
            return "本会话还没有报告。"
        version = arguments.get("version")
        report = store.load(scope.session_id, rid, int(version) if version else None)
        if report is None:
            return f"错误: 本会话没有报告 {rid}" + (f" 的 v{version}" if version else "") + "。"
        return store.dump_for_model(report)


class ReportToolPack(ToolPack):
    pack_name = "report"
    description = "报告卡片：publish_report / update_report / read_report"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        cfg = config or {}
        export.configure(cfg.get("export"), {"risk_note": cfg.get("risk_note"), "footer": cfg.get("footer")})
        self._tools = [PublishReportTool(), UpdateReportTool(), ReadReportTool()]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
