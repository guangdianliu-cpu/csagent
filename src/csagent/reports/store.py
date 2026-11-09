"""报告按会话落盘：``data/uploads/<会话>/outbound/reports/<report_id>/v<n>.json``。

每次更新写一个新版本文件，旧版本原样保留，面板可以切回去。会话目录就是读权限的边界：
接口只按调用者自己的会话去找，别的会话的 report_id 拿到了也读不到。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

import structlog

from csagent.channels.workspace_uploads import session_outbound_dir, workspace_relative
from csagent.reports.model import Report, ReportContent

logger = structlog.get_logger(__name__)

REPORTS_DIRNAME = "reports"
_ID_RE = re.compile(r"^rpt_[0-9a-f]{8,16}$")
_VERSION_RE = re.compile(r"^v(\d+)\.json$")


def valid_report_id(report_id: str) -> bool:
    """只认我们自己发的 id，顺带挡住路径穿越。"""
    return bool(_ID_RE.match(report_id or ""))


def new_report_id() -> str:
    return f"rpt_{secrets.token_hex(4)}"


def reports_dir(session_id: str, *, root: Path | None = None) -> Path:
    return session_outbound_dir(session_id, root=root) / REPORTS_DIRNAME


def _report_dir(session_id: str, report_id: str, *, root: Path | None = None) -> Path:
    if not valid_report_id(report_id):
        raise ValueError(f"不是有效的报告 id：{report_id}")
    return reports_dir(session_id, root=root) / report_id


def versions(session_id: str, report_id: str, *, root: Path | None = None) -> list[int]:
    """该报告已有的版本号，升序；报告不存在返回空列表。"""
    try:
        d = _report_dir(session_id, report_id, root=root)
    except ValueError:
        return []
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _VERSION_RE.match(p.name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def load(
    session_id: str, report_id: str, version: int | None = None, *, root: Path | None = None
) -> Report | None:
    """读一版；``version`` 为空读最新。不存在或文件坏了返回 None。"""
    have = versions(session_id, report_id, root=root)
    if not have:
        return None
    v = have[-1] if version is None else version
    if v not in have:
        return None
    path = _report_dir(session_id, report_id, root=root) / f"v{v}.json"
    try:
        return Report.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("report_load_failed", path=str(path), error=str(exc))
        return None


def latest_id(session_id: str, *, root: Path | None = None) -> str:
    """本会话最近改过的报告；没有返回空串。"""
    d = reports_dir(session_id, root=root)
    if not d.is_dir():
        return ""
    candidates = [p for p in d.iterdir() if p.is_dir() and valid_report_id(p.name)]
    if not candidates:
        return ""
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def save(
    session_id: str,
    content: ReportContent,
    *,
    report_id: str = "",
    root: Path | None = None,
) -> tuple[Report, Path]:
    """写一个新版本：没给 ``report_id`` 就新建报告（v1），给了就在最新版本上 +1。

    :returns: (落盘的报告, 文件路径)
    """
    rid = report_id or new_report_id()
    d = _report_dir(session_id, rid, root=root)
    d.mkdir(parents=True, exist_ok=True)
    have = versions(session_id, rid, root=root)
    report = Report(
        **content.model_dump(),
        report_id=rid,
        version=(have[-1] + 1) if have else 1,
        created_at=time.time(),
    )
    path = d / f"v{report.version}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(report.model_dump_json(), encoding="utf-8")
    os.replace(tmp, path)
    # 目录 mtime 跟着最新版本走，latest_id 靠它判断「最近改过的」
    os.utime(d)
    return report, path


def relative_path(path: Path) -> str:
    """工作区相对路径（与产物清单同一口径）。"""
    return workspace_relative(path)


def dump_for_model(report: Report) -> str:
    """给 read_report 用：模型改报告时要拿到完整积木。"""
    return json.dumps(report.model_dump(exclude={"created_at"}), ensure_ascii=False)
