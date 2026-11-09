"""会话临时工作区清理：uploads 与 scripts/tools 下的过期会话产物。

清理范围（按目录/文件 mtime）：
- data/uploads/{session}/          — inbound / outbound / .active_inbound
- scripts/tools/{session}/         — write_script 产生的临时脚本
- scripts/tools/ 根下散落的文件    — 误写入的 xlsx 等（不含子目录）

不清理：data/audit、skills/、configs/、src/。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from csagent.infra.config import PROJECT_ROOT

logger = structlog.get_logger(__name__)


@dataclass
class CleanupReport:
    removed_dirs: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.removed_dirs) + len(self.removed_files)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dir_activity_mtime(path: Path) -> float:
    """目录活跃时间：自身 mtime 与直接子项 mtime 的最大值（不深扫大树）。"""
    latest = _mtime(path)
    try:
        for child in path.iterdir():
            latest = max(latest, _mtime(child))
            if child.is_dir():
                try:
                    for nested in child.iterdir():
                        latest = max(latest, _mtime(nested))
                except OSError:
                    continue
    except OSError:
        pass
    return latest


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def cleanup_workspace(
    *,
    project_root: Path | None = None,
    uploads_root: Path | None = None,
    scripts_dir: Path | None = None,
    ttl_days: float = 7.0,
    dry_run: bool = False,
    now: float | None = None,
) -> CleanupReport:
    """删除超过 TTL 的会话工作区与临时脚本。ttl_days<=0 时不清理。"""
    report = CleanupReport()
    if ttl_days <= 0:
        return report

    root = (project_root or PROJECT_ROOT).resolve()
    uploads = (uploads_root or (root / "data" / "uploads")).resolve()
    scripts = (scripts_dir or (root / "scripts" / "tools")).resolve()
    cutoff = (now if now is not None else time.time()) - float(ttl_days) * 86400.0

    if uploads.is_dir():
        _cleanup_session_children(uploads, root, cutoff, dry_run, report)
    if scripts.is_dir():
        _cleanup_session_children(scripts, root, cutoff, dry_run, report)
        _cleanup_loose_files(scripts, root, cutoff, dry_run, report)

    if report.removed_count or report.errors:
        logger.info(
            "workspace_cleanup_done",
            dry_run=dry_run,
            ttl_days=ttl_days,
            removed_dirs=len(report.removed_dirs),
            removed_files=len(report.removed_files),
            errors=len(report.errors),
        )
    return report


def _cleanup_session_children(
    parent: Path,
    project_root: Path,
    cutoff: float,
    dry_run: bool,
    report: CleanupReport,
) -> None:
    try:
        children = list(parent.iterdir())
    except OSError as exc:
        report.errors.append(f"{parent}: {exc}")
        return
    for child in children:
        if not child.is_dir():
            continue
        # 保留误建的顶层 data 目录名以外的会话目录；scripts/tools/data 也清
        activity = _dir_activity_mtime(child)
        if activity >= cutoff:
            report.skipped.append(_rel(child, project_root))
            continue
        rel = _rel(child, project_root)
        if dry_run:
            report.removed_dirs.append(rel)
            continue
        try:
            shutil.rmtree(child)
            report.removed_dirs.append(rel)
        except OSError as exc:
            report.errors.append(f"{rel}: {exc}")


def _cleanup_loose_files(
    parent: Path,
    project_root: Path,
    cutoff: float,
    dry_run: bool,
    report: CleanupReport,
) -> None:
    """清理 scripts/tools 根下散落文件（不含子目录）。"""
    try:
        children = list(parent.iterdir())
    except OSError as exc:
        report.errors.append(f"{parent}: {exc}")
        return
    for child in children:
        if not child.is_file():
            continue
        if _mtime(child) >= cutoff:
            report.skipped.append(_rel(child, project_root))
            continue
        rel = _rel(child, project_root)
        if dry_run:
            report.removed_files.append(rel)
            continue
        try:
            child.unlink()
            report.removed_files.append(rel)
        except OSError as exc:
            report.errors.append(f"{rel}: {exc}")


def main() -> None:
    """CLI：python -m csagent.channels.workspace_cleanup [--ttl-days N] [--dry-run]"""
    import argparse
    import json

    from csagent.infra.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="清理过期的 uploads / scripts/tools 会话产物")
    parser.add_argument(
        "--ttl-days",
        type=float,
        default=settings.workspace_cleanup_ttl_days,
        help="保留天数（默认读 configs/base.yaml）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出将删除项")
    args = parser.parse_args()
    report = cleanup_workspace(ttl_days=args.ttl_days, dry_run=args.dry_run)
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "ttl_days": args.ttl_days,
                "removed_dirs": report.removed_dirs,
                "removed_files": report.removed_files,
                "errors": report.errors,
                "skipped_count": len(report.skipped),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
