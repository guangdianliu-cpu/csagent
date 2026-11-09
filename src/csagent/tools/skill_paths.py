"""Skill 资源路径解析：`<skill:name>/rel_path` → Skill 目录内的绝对路径（只读）。

被 workspace_files_pack.py（read_file）和 script_pack.py（run_script）共享，
避免两个 ToolPack 互相 import。
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL_PATH_RE = re.compile(
    r"^<skill:([a-z0-9]+(?:-[a-z0-9]+)*)>(?:/(.*))?$"
)


def _resolve_skill_path(raw: str) -> tuple[str, str] | None:
    """尝试解析 <skill:name> 或 <skill:name>/rel_path。返回 (name, rel_path) 或 None。

    仅写 Skill 名时 rel_path 为 ``.``，用于 list_dir Skill 根目录。
    """
    m = _SKILL_PATH_RE.match(str(raw or "").strip())
    if not m:
        return None
    rel = m.group(2)
    return m.group(1), (rel if rel not in (None, "") else ".")


def is_skill_path(raw: str) -> bool:
    """判断是否为 Skill 路径。"""
    return _SKILL_PATH_RE.match(str(raw or "").strip()) is not None


# 全局 Skill Discovery getter（由 main.py 设置）
_skill_discovery_getter_fn = None


def set_skill_discovery_getter(fn) -> None:
    """设置获取 SkillDiscovery 的函数（延迟绑定，避免模块级循环 import）。"""
    global _skill_discovery_getter_fn
    _skill_discovery_getter_fn = fn


def _skill_discovery_var_getter():
    if _skill_discovery_getter_fn is not None:
        return _skill_discovery_getter_fn()
    return None


def skill_roots() -> list[Path]:
    """当前生效的 Skill 根目录列表；Skill 系统未初始化时返回空列表。

    供脚本运行时把 Skill 根以环境变量交给子进程，免得模型靠猜路径去定位
    Skill 自带的数据文件。
    """
    discovery = _skill_discovery_var_getter()
    if discovery is None:
        return []
    try:
        return [Path(r).resolve() for r in discovery.roots]
    except (AttributeError, OSError):
        return []


def skill_python_paths() -> list[Path]:
    """已启用 Skill 在 frontmatter 里声明的 python-path 目录，按 Skill 名排序。

    run_script 把它们放进子进程 PYTHONPATH，会话脚本可以直接 ``from lib import …``。
    多个 Skill 有同名模块时排在前面的生效，所以给会话脚本 import 的模块名要起得独特。
    硬约束模式下只给本轮加载过的 Skill，与 resolve_skill_resource 同一口径。
    """
    discovery = _skill_discovery_var_getter()
    if discovery is None:
        return []
    from csagent.tools.delegation import current_delegation

    delegation = current_delegation()
    try:
        packages = sorted(discovery.scan(), key=lambda p: p.name)
    except AttributeError:
        return []
    paths: list[Path] = []
    for pkg in packages:
        if not pkg.enabled or not getattr(pkg, "python_path", None):
            continue
        if delegation is not None and delegation.enforced and pkg.name not in delegation.loaded_skills:
            continue
        root = pkg.root.resolve()
        for rel in pkg.python_path:
            candidate = (root / rel).resolve()
            if candidate.is_dir() and candidate.is_relative_to(root):
                paths.append(candidate)
    return paths


def resolve_skill_resource(raw: str) -> Path | str:
    """解析 Skill 资源路径 <skill:name>/rel_path → 绝对路径（只读）。"""
    parsed = _resolve_skill_path(raw)
    if parsed is None:
        return f"错误: 无法解析 Skill 路径 '{raw}'"
    name, rel_path = parsed

    from csagent.skills.discovery import SkillDiscovery  # noqa: F401  (类型提示/文档用途)

    discovery = _skill_discovery_var_getter()
    if discovery is None:
        return "错误: Skill 系统未初始化"

    pkg = discovery.get(name)
    if pkg is None:
        return f"错误: 未找到 Skill '{name}'"
    if not pkg.enabled:
        return f"错误: Skill '{name}' 已被禁用（enabled: false）"
    # 硬约束模式（enforce_allowed_tools）下只能访问本轮已加载的 Skill 的资源：
    # 以前白名单只管工具名，run_script / read_file 仍能读任意其他 Skill 的文件（D11）
    from csagent.tools.delegation import current_delegation

    delegation = current_delegation()
    if delegation is not None and delegation.enforced and name not in delegation.loaded_skills:
        return f"错误: 本轮未加载 Skill '{name}'，不能访问它的资源；先 load_skill('{name}')"

    candidate = (pkg.root / rel_path).resolve()
    try:
        candidate.relative_to(pkg.root.resolve())
    except ValueError:
        return "错误: 路径越出 Skill 目录"
    return candidate
