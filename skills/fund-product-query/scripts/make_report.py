"""把 multi_lookup 的落盘结果转成一张可以直接发给用户的 xlsx。

为什么要有它：名单一大（>15 只），把结果排成 Markdown 表格发进企微，
手机上根本看不了；而让模型现场写 openpyxl 脚本，线上实测要多烧 3~5 轮
（找路径、试字段、再写打印脚本）。所以固定成 Skill 自带脚本，一次调用出表。

用法（先用 product_lookup 的 out_file 落盘，再喂给它；两个都是 tools.yaml 声明的原生工具）：

    product_lookup(fund_codes=[...], aspects=["basic", "limits"], out_file="funds.json")
    product_make_report(source="funds.json", target="代销核对.xlsx")
    send_file(path="代销核对.xlsx")

原生工具经 stdin 传参（``--payload -``）；调试时也可以直接跑
``python make_report.py funds.json 代销核对.xlsx``。成功输出 JSON，exit 0；
失败输出 {"ok": false, "error": {"code", "message"}}，exit 1。

输入输出都落在本会话的 outbound 目录（CSAGENT_OUTBOUND_DIR），只认裸文件名。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# 表头 → 取值来源。顺序就是列顺序；对客表格不堆字段，够判断「能不能买、买多少」即可。
_COLUMNS: list[tuple[str, str, str]] = [
    ("产品名称", "basic", "fund_name"),
    ("基金代码", "basic", "fund_code"),
    ("类型", "basic", "fund_type_name"),
    ("风险等级", "basic", "risky_name"),
    ("是否代销", "basic", "consign_flag"),
    ("基金状态", "basic", "fundstate"),
    ("申购状态", "basic", "buystatus"),
    ("赎回状态", "basic", "redeemstatus"),
    ("单位净值", "basic", "nav_display"),
    ("净值日期", "basic", "nav_update_date"),
    ("起购金额(元)", "limits", "minvalue"),
    ("单日累计上限(元)", "limits", "maxvalue"),
    ("管理人", "basic", "manager_name"),
]

# 状态码 → 对客中文。空值一律是「未返回」，不能当成「可以」，与 SKILL.md 口径一致。
_CONSIGN = {"1": "是", "0": "否"}
_BUYSTATE = {"1": "可申购", "0": "已关闭"}
_REDEEMSTATE = {"1": "可赎回", "0": "已关闭"}
_FUNDSTATE = {
    "0": "正常", "3": "发行失败", "4": "停止交易",
    "5": "停止申购", "8": "基金终止", "9": "基金封闭",
}

# 与 SKILL.md 一致：天文数字不念，写成「暂无明确上限」。
_NO_LIMIT = 9999999999


def _resolve(name: str, *, must_exist: bool) -> Path:
    """裸文件名 → 本会话 outbound 目录下的绝对路径。"""
    base = os.environ.get("CSAGENT_OUTBOUND_DIR") or "."
    path = Path(base) / Path(str(name).replace("\\", "/")).name
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"找不到 {path.name}（应先用 product_lookup 的 out_file 落盘）")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _amount(value: Any) -> Any:
    if value is None or value == "":
        return "未返回"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num >= _NO_LIMIT:
        return "暂无明确上限"
    return int(num) if num == int(num) else num


def _label(value: Any, table: dict[str, str]) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return "未返回"
    return table.get(text, text)


def _rows_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 multi_lookup 的两种形状：完整视图 funds 是对象，降级视图是数组。"""
    funds = data.get("funds")
    if isinstance(funds, list):
        return [{"basic": r, "limits": r, "failed": r.get("failed") or []} for r in funds]
    out = []
    for code, blk in (funds or {}).items():
        basic = dict((blk.get("basic") or [{}])[0])
        basic.setdefault("fund_code", code)
        out.append({
            "basic": basic,
            "limits": (blk.get("limits") or [{}])[0],
            "failed": blk.get("failed") or [],
        })
    return out


def build_rows(data: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in _rows_from(data):
        line: list[Any] = []
        for _header, source, field in _COLUMNS:
            raw = item.get(source, {}).get(field)
            if source in item["failed"] and field != "fund_code":
                # 接口调用失败 ≠ 未返回：表里要能看出这一格是没查到，不是数据源没有
                line.append("查询失败")
            elif field == "consign_flag":
                line.append(_label(raw, _CONSIGN))
            elif field == "buystatus":
                line.append(_label(raw, _BUYSTATE))
            elif field == "redeemstatus":
                line.append(_label(raw, _REDEEMSTATE))
            elif field == "fundstate":
                line.append(_label(raw, _FUNDSTATE))
            elif field in ("minvalue", "maxvalue"):
                line.append(_amount(raw))
            else:
                line.append("未返回" if raw in (None, "") else raw)
        rows.append(line)
    return rows


def write_xlsx(rows: list[list[Any]], dest: Path, *, custtype: str = "0") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "代销核对"

    headers = [c[0] for c in _COLUMNS]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F3864")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in rows:
        ws.append(row)

    for idx, header in enumerate(headers, start=1):
        widest = max([len(str(header))] + [len(str(r[idx - 1])) for r in rows] or [0])
        # 中文按 1.6 倍估宽，够看又不至于把列拉得离谱
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = min(
            40, max(10, int(widest * 1.6))
        )
    ws.freeze_panes = "A2"

    ws.append([])
    ws.append([f"客户类型口径：{'机构' if str(custtype) == '0' else '个人'}；数据来源：产品中心"])
    ws.append(["「未返回」表示产品中心未给出该字段状态，不等于「否」或「不可」"])
    ws.append(["「查询失败」表示本次调用产品中心接口出错、没查到，不等于「未返回」，可稍后重新核对"])

    wb.save(dest)


def _fail(code: str, message: str) -> int:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False))
    return 1


def _args_from_stdin() -> list[str]:
    """原生工具的入参：stdin 里的 {"source": ..., "target": ...} 还原成位置参数。"""
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return [str(payload[k]) for k in ("source", "target") if payload.get(k)]


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--payload", "-"]:
        try:
            args = _args_from_stdin()
        except ValueError as exc:
            return _fail("BAD_PAYLOAD", f"参数错误: {exc}")
    if not args:
        return _fail("BAD_PAYLOAD", "参数错误: 缺少 source（product_lookup 用 out_file 落盘的文件名，如 funds.json）")

    try:
        src = _resolve(args[0], must_exist=True)
    except FileNotFoundError as exc:
        return _fail("BAD_PAYLOAD", f"参数错误: {exc}")

    dest_name = args[1] if len(args) > 1 else "基金核对结果.xlsx"
    if not dest_name.lower().endswith(".xlsx"):
        dest_name += ".xlsx"
    dest = _resolve(dest_name, must_exist=False)

    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail("BAD_PAYLOAD", f"读取失败: {src.name} 不是合法 JSON（{exc}）")

    rows = build_rows(data)
    if not rows:
        return _fail("NO_DATA", f"{src.name} 里没有基金数据，未生成报表")

    try:
        write_xlsx(rows, dest, custtype=str(data.get("custtype") or "0"))
    except ImportError:
        return _fail("ENV_MISSING", "生成失败: 环境缺少 openpyxl")
    except OSError as exc:
        return _fail("WRITE_FAILED", f"生成失败: {exc}")

    print(json.dumps({
        "saved_to": dest.name,
        "rows": len(rows),
        "columns": [c[0] for c in _COLUMNS],
        "note": f"报表已生成，用 send_file(path=\"{dest.name}\") 发给用户",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
