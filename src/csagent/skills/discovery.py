"""Skill 包发现与索引：多根扫描、校验、冲突处理、热重载。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import structlog

from csagent.common.exceptions import SkillValidationError
from csagent.skills.package import SkillPackage, parse_skill_md

logger = structlog.get_logger(__name__)


class SkillDiscovery:
    """从多个根目录发现、校验并缓存 Skill 包。

    roots 优先级从低到高：列表中靠后的根覆盖靠前的同名 Skill。
    """

    def __init__(
        self, roots: list[Path], *, allowlist: frozenset[str] | None = None
    ) -> None:
        """:param allowlist: 当前部署形态可见的 Skill 名单。给了就以它决定 enabled，
            忽略 SKILL.md 的开关；None 表示沿用 SKILL.md。
        """
        self._roots = roots
        self._allowlist = allowlist
        self._cache: list[SkillPackage] | None = None
        self._by_name: dict[str, SkillPackage] = {}

    @property
    def roots(self) -> list[Path]:
        """扫描根目录（只读副本）。"""
        return list(self._roots)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def scan(self) -> list[SkillPackage]:
        """扫描+校验+去重，结果缓存。"""
        if self._cache is not None:
            return list(self._cache)
        return self._do_scan()

    def reload(self) -> list[SkillPackage]:
        """清缓存重扫，返回最新列表。"""
        self._cache = None
        self._by_name.clear()
        return self._do_scan()

    def get(self, name: str) -> SkillPackage | None:
        """按名称获取 Skill 包。"""
        if self._cache is None:
            self._do_scan()
        return self._by_name.get(name)

    def render_index(self, *, max_chars: int = 4000, resident: Iterable[str] = ()) -> str:
        """渲染 L1 索引文本；超预算时按 name 字典序截断并追加提示。

        :param resident: 常驻 Skill（说明已在系统提示词里），索引里标明不用 load_skill
        """
        packages = self.scan()
        enabled = [p for p in packages if p.enabled]
        if not enabled:
            return ""
        resident_names = set(resident)

        header = (
            "## 可用 Skill\n"
            "调用 load_skill(name) 获取某个 Skill 的完整操作说明后再执行任务"
            + ("（标「已常驻」的不用，说明已在本提示词末尾）。\n" if resident_names else "。\n")
        )
        lines: list[str] = []
        total_chars = len(header)
        truncated = False

        for pkg in sorted(enabled, key=lambda p: p.name):
            mark = "（已常驻）" if pkg.name in resident_names else ""
            line = f"- {pkg.name}{mark}: {pkg.description}\n"
            if total_chars + len(line) > max_chars:
                truncated = True
                break
            lines.append(line)
            total_chars += len(line)

        result = header + "".join(lines)
        if truncated:
            result += f"（共 {len(enabled)} 个 Skill，仅显示部分，请用 list_skills 查看全部）\n"
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _do_scan(self) -> list[SkillPackage]:
        """执行实际扫描。"""
        # source 标签映射
        source_labels = self._build_source_labels()

        # 收集所有候选：后出现的根优先级更高
        candidates: dict[str, SkillPackage] = {}

        for root_idx, root in enumerate(self._roots):
            resolved = root.resolve()
            if not resolved.is_dir():
                logger.debug("skill_root_not_found", root=str(resolved))
                continue

            source = source_labels.get(root_idx, "builtin")

            for child in sorted(resolved.iterdir()):
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue

                try:
                    text = skill_md.read_text(encoding="utf-8")
                    pkg = parse_skill_md(text, root=child, source=source)
                except SkillValidationError as exc:
                    logger.error(
                        "skill_validation_failed",
                        path=str(skill_md),
                        error=str(exc),
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "skill_load_failed",
                        path=str(skill_md),
                        error=str(exc),
                    )
                    continue

                if pkg.name in candidates:
                    prev = candidates[pkg.name]
                    logger.warning(
                        "skill_name_conflict",
                        name=pkg.name,
                        winner=str(child),
                        loser=str(prev.root),
                    )

                candidates[pkg.name] = pkg

        result = self._apply_profile_allowlist(list(candidates.values()))
        self._cache = result
        self._by_name = {p.name: p for p in result}

        logger.info(
            "skill_scan_complete",
            count=len(result),
            names=[p.name for p in result],
        )
        return list(result)

    def _apply_profile_allowlist(self, packages: list[SkillPackage]) -> list[SkillPackage]:
        """形态给了白名单时，以它决定 enabled，忽略 SKILL.md 的开关。

        这样切换形态不用再去改各个 SKILL.md（那会把工作区弄脏、拉代码时冲突）。
        """
        allow = self._allowlist
        if allow is None:
            return packages
        unknown = sorted(allow - {p.name for p in packages})
        if unknown:
            logger.warning("skill_allowlist_unknown", names=unknown)
        return [p.model_copy(update={"enabled": p.name in allow}) for p in packages]

    def _build_source_labels(self) -> dict[int, str]:
        """根据根目录路径猜测 source 标签。"""
        labels: dict[int, str] = {}
        for idx, root in enumerate(self._roots):
            root_str = str(root)
            if ".csagent" in root_str and "~" not in root_str:
                labels[idx] = "project"
            elif "~" in root_str or "home" in root_str.lower() or ".csagent" in root_str:
                labels[idx] = "user"
            else:
                labels[idx] = "builtin"
        return labels
