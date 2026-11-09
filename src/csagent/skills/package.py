"""Skill 包模型与 SKILL.md 解析。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from csagent.common.exceptions import SkillValidationError

logger = structlog.get_logger(__name__)

# Skill 根目录下声明原生工具的清单（见 SkillToolSpec）
TOOLS_MANIFEST = "tools.yaml"

# 工具名：与 function calling 的函数名规则兼容，统一用小写下划线
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# YAML frontmatter 分隔符
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# 正文长度告警阈值
_BODY_WARN_CHARS = 20_000

# description 最短字符数
_DESC_MIN_CHARS = 20

# name 最大字符数
_NAME_MAX_CHARS = 64

# name 格式（kebab-case）
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillToolSpec(BaseModel):
    """Skill 在 tools.yaml 里声明的一个原生工具。

    内核把它注册成 function calling 工具：模型按 ``parameters``（JSON Schema）给参数，
    内核把参数 JSON 原样写进脚本 stdin，脚本 stdout 就是工具结果，退出码非 0 记为调用失败。
    """

    name: str
    description: str
    script: str  # 相对 Skill 根，必须在 scripts/ 下
    argv: list[str] = Field(default_factory=list)
    parameters: dict[str, Any]


class SkillPackage(BaseModel):
    """一个 Skill 包的完整描述。"""

    name: str
    description: str
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None  # None = 不限制
    enabled: bool = True
    root: Path  # skill 目录绝对路径
    body: str  # SKILL.md 去 frontmatter 的正文
    resources: list[str] = Field(default_factory=list)  # 相对 root 的资源路径
    source: str  # "builtin" | "user" | "project"
    tools: list[SkillToolSpec] = Field(default_factory=list)  # tools.yaml 声明的原生工具
    # 会话脚本可以直接 import 的模块目录（相对 root），由 run_script 放进 PYTHONPATH
    python_path: list[str] = Field(default_factory=list)


def _parse_yaml_frontmatter(text: str) -> dict:
    """手动解析简单 YAML frontmatter，避免引入额外依赖。

    仅支持：字符串、列表（[a, b]）、布尔、数字。
    """
    import yaml

    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillValidationError("缺少 YAML frontmatter（文件必须以 --- 开头）")
    raw = match.group(1)
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:
        raise SkillValidationError(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillValidationError("frontmatter 必须是 key-value 映射")
    return data


def _extract_body(text: str) -> str:
    """提取 frontmatter 之后的正文。"""
    match = _FRONTMATTER_RE.match(text)
    if match:
        return text[match.end():]
    return text


# 编译产物 / 编辑器残留：会跟着 rglob 一起被收进资源清单，进而出现在
# load_skill 注入模型的「本 Skill 携带的资源」里。9 个 .pyc 白占上下文，
# 还可能诱导模型去 read_file 一个二进制文件。
_RESOURCE_SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints", ".pytest_cache"}
_RESOURCE_SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".dll"}


def _collect_resources(root: Path) -> list[str]:
    """收集 scripts/ references/ assets/ 下的资源文件，返回相对路径列表。"""
    resources: list[str] = []
    for subdir in ("scripts", "references", "assets"):
        d = root / subdir
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            if _RESOURCE_SKIP_DIRS & set(p.parts):
                continue
            if p.suffix.lower() in _RESOURCE_SKIP_SUFFIXES:
                continue
            resources.append(str(p.relative_to(root)).replace("\\", "/"))
    return resources


def parse_skill_md(text: str, *, root: Path, source: str) -> SkillPackage:
    """解析 SKILL.md；frontmatter 缺失或非法时抛 SkillValidationError。"""
    data = _parse_yaml_frontmatter(text)

    # --- 必填字段校验 ---
    name = str(data.get("name") or "").strip()
    if not name:
        raise SkillValidationError("缺少必填字段 'name'")

    description = str(data.get("description") or "").strip()
    if not description:
        raise SkillValidationError("缺少必填字段 'description'")

    # name 格式与长度
    if len(name) > _NAME_MAX_CHARS:
        raise SkillValidationError(
            f"name '{name}' 超过 {_NAME_MAX_CHARS} 字符上限"
        )

    # name 必须等于目录名
    dir_name = root.name
    if name != dir_name:
        raise SkillValidationError(
            f"name '{name}' 与目录名 '{dir_name}' 不一致"
        )

    # description 最短长度
    if len(description) < _DESC_MIN_CHARS:
        raise SkillValidationError(
            f"description 过短（{len(description)} 字符，最少 {_DESC_MIN_CHARS}）"
        )

    # --- 正文 ---
    body = _extract_body(text).strip()
    if len(body) > _BODY_WARN_CHARS:
        logger.warning(
            "skill_body_too_long",
            name=name,
            chars=len(body),
            hint="建议拆分到 references/ 子目录",
        )

    # --- 可选字段 ---
    version = str(data.get("version", "1.0.0") or "1.0.0")
    tags_raw = data.get("tags")
    tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    allowed_tools_raw = data.get("allowed-tools") or data.get("allowed_tools")
    allowed_tools = (
        [str(t) for t in allowed_tools_raw]
        if isinstance(allowed_tools_raw, list)
        else None
    )
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).lower() in ("true", "1", "yes")

    resources = _collect_resources(root)

    return SkillPackage(
        name=name,
        description=description,
        version=version,
        tags=tags,
        allowed_tools=allowed_tools,
        enabled=enabled,
        root=root,
        body=body,
        resources=resources,
        source=source,
        tools=_load_tool_specs(root, name),
        python_path=_parse_python_path(data.get("python-path") or data.get("python_path"), root, name),
    )


def _parse_python_path(raw: Any, root: Path, skill: str) -> list[str]:
    """frontmatter 的 python-path：Skill 内的相对目录。写错的条目丢掉并报错，不连累整个 Skill。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    paths: list[str] = []
    for item in items:
        rel = str(item).strip().replace("\\", "/")
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or not (root / rel).is_dir():
            logger.error("skill_python_path_invalid", skill=skill, path=rel)
            continue
        paths.append(rel)
    return paths


def _load_tool_specs(root: Path, skill: str) -> list[SkillToolSpec]:
    """读 Skill 根目录的 tools.yaml。单个工具写错只跳过它并报错，不连累整个 Skill。"""
    path = root / TOOLS_MANIFEST
    if not path.is_file():
        return []
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("skill_tools_manifest_invalid", skill=skill, error=str(exc))
        return []
    items = raw.get("tools") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        logger.error("skill_tools_manifest_invalid", skill=skill, error="缺少 tools 列表")
        return []

    specs: list[SkillToolSpec] = []
    for item in items:
        try:
            spec = _parse_tool_spec(item, root)
        except SkillValidationError as exc:
            logger.error("skill_tool_invalid", skill=skill, error=str(exc))
            continue
        if any(s.name == spec.name for s in specs):
            logger.error("skill_tool_invalid", skill=skill, error=f"工具名重复: {spec.name}")
            continue
        specs.append(spec)
    return specs


def _parse_tool_spec(item: Any, root: Path) -> SkillToolSpec:
    if not isinstance(item, dict):
        raise SkillValidationError("tools 里的每一项必须是映射")
    name = str(item.get("name") or "").strip()
    if not _TOOL_NAME_RE.match(name):
        raise SkillValidationError(f"工具名 {name!r} 不合法：小写字母开头，只含小写字母、数字、下划线")
    description = str(item.get("description") or "").strip()
    if not description:
        raise SkillValidationError(f"工具 {name} 缺少 description")
    script = str(item.get("script") or "").strip().replace("\\", "/")
    if not script.startswith("scripts/") or ".." in Path(script).parts:
        raise SkillValidationError(f"工具 {name} 的 script 必须是 scripts/ 下的相对路径: {script!r}")
    if not (root / script).is_file():
        raise SkillValidationError(f"工具 {name} 的脚本不存在: {script}")
    argv = item.get("argv") or []
    if not isinstance(argv, list):
        raise SkillValidationError(f"工具 {name} 的 argv 必须是列表")
    parameters = item.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise SkillValidationError(f"工具 {name} 的 parameters 必须是 type: object 的 JSON Schema")
    return SkillToolSpec(
        name=name,
        description=description,
        script=script,
        argv=[str(a) for a in argv],
        parameters=parameters,
    )
