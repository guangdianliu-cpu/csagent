"""Skill 工具包：list_skills + load_skill。

模型通过这两个工具实现「看见 Skill 目录、按需展开」的渐进式披露。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.skills.package import SkillPackage
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)

# load_skill 注入上下文的正文上限。
# 取 12000 是因为现有 Skill 里除 fund-product-query(31KB) 外最长的是 15KB，
# 而那份 15KB 的用起来没出现「反复找路」的症状——12000 能兜住正常的、
# 只对失控的那种生效。超了不是报错，是截断 + 告知续读方式。
_MAX_SKILL_BODY_CHARS = 12000


class ListSkillsTool(BaseTool):
    """列出可用 Skill 及其用途。"""

    name = "list_skills"
    description = "列出可用 Skill 及其用途。可传 query 做模糊过滤。"

    def __init__(self, discovery: SkillDiscovery) -> None:
        self._discovery = discovery

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "可选的过滤关键词",
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        query = str(arguments.get("query") or "").strip().lower()
        packages = self._discovery.scan()
        enabled = [p for p in packages if p.enabled]

        if query:
            filtered = [
                p for p in enabled
                if query in p.name.lower()
                or query in p.description.lower()
                or any(query in t.lower() for t in p.tags)
            ]
        else:
            filtered = enabled

        if not filtered:
            if query:
                return f"没有找到匹配 '{query}' 的 Skill。共有 {len(enabled)} 个可用 Skill。"
            return "当前没有可用的 Skill。"

        lines = [f"- {p.name}: {p.description}" for p in sorted(filtered, key=lambda p: p.name)]
        header = f"共 {len(filtered)} 个 Skill：\n" if len(filtered) > 1 else ""
        return header + "\n".join(lines)


def render_skill_text(pkg: SkillPackage) -> str:
    """Skill 给模型看的说明：正文（超长截断）+ 资源清单 + 声明的原生工具。load_skill 与常驻共用。"""
    name = pkg.name
    # 正文过长会适得其反：实测 31KB 的 SKILL.md 塞进上下文后，模型反而在里面
    # 找不到关键约束，转头去 list_dir、读脚本源码、分页重读同一份文档——
    # 一轮问答 29 次工具调用，其中约六成是这种「找路」而非干活。
    # 超限就截断并明确告知怎么取全文，让模型有确定的下一步，而不是自己摸索。
    body = pkg.body
    if len(body) > _MAX_SKILL_BODY_CHARS:
        body = (
            body[:_MAX_SKILL_BODY_CHARS]
            + f"\n\n…[本 Skill 说明超过 {_MAX_SKILL_BODY_CHARS} 字符已截断。"
            f"需要后续内容时用 read_file(path=\"<skill:{name}>/SKILL.md\", offset=…) 续读；"
            "但先确认上面的内容是否已够用，多数情况够。]"
        )
        logger.warning(
            "skill_body_truncated",
            skill=name,
            original_chars=len(pkg.body),
            limit=_MAX_SKILL_BODY_CHARS,
        )
    parts: list[str] = [body]

    # 资源清单：正文里已经写了路径的不再列。常驻时这段每轮都在提示词里，以前把正文表格里
    # 写过的 reference 又列一遍，还给正文明令「不要读」的实现文件配上 run_script 提示
    unlisted = [res for res in pkg.resources if res not in body]
    if unlisted:
        parts.append("\n## 本 Skill 携带的资源（可直接使用）")
        for res in unlisted:
            if res.startswith("scripts/"):
                parts.append(
                    f"- {res}   → run_script(script=\"<skill:{name}>/{res}\", ...)"
                )
            else:
                parts.append(
                    f"- {res} → read_file(path=\"<skill:{name}>/{res}\")"
                )

    # tools.yaml 声明的原生工具：已注册成普通工具，这里告诉模型直接调用
    if pkg.tools:
        parts.append("\n## 本 Skill 提供的工具（直接调用，参数按工具定义传，不要经 run_script）")
        parts.extend(f"- {spec.name}" for spec in pkg.tools)
    return "\n".join(parts)


def render_resident_skills(discovery: SkillDiscovery, names: Iterable[str]) -> str:
    """常驻 Skill 的完整说明，由 Agent 放进系统提示词末尾。

    形态里几乎每个问题都要用的 Skill，以前每问先花一个来回 load_skill 再干活
    （见 2026-10-22 审计：一天 24 个来回只在做准备）。常驻后说明一直在上下文里。
    不存在或未启用的名字直接跳过（启动时 main 已报错）。
    """
    blocks: list[str] = []
    for name in names:
        pkg = discovery.get(name)
        if pkg is None or not pkg.enabled:
            continue
        blocks.append(
            f"## 已常驻的 Skill：{name}\n\n"
            f"下面就是 {name} 的完整说明，已经在上下文里，不用再调 load_skill。\n\n"
            + render_skill_text(pkg)
        )
    return "\n\n".join(blocks)


class LoadSkillTool(BaseTool):
    """加载指定 Skill 的完整操作说明。"""

    name = "load_skill"
    description = "加载指定 Skill 的完整操作说明。执行该领域任务前必须先加载。"

    def __init__(
        self,
        discovery: SkillDiscovery,
        *,
        enforce_allowed_tools: bool = False,
        resident: Iterable[str] = (),
    ) -> None:
        self._discovery = discovery
        self._enforce = enforce_allowed_tools
        # 常驻 Skill 的说明已在系统提示词里，load_skill 只回一句，不再把正文再塞一遍
        self._resident = frozenset(resident)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return "错误: 必须指定 Skill 名称。"

        pkg = self._discovery.get(name)
        if pkg is None:
            # 提供最接近的候选
            candidates = self._find_similar(name, top_k=3)
            msg = f"未找到名为 '{name}' 的 Skill。"
            if candidates:
                msg += "\n最接近的候选：" + ", ".join(candidates)
            return msg

        if not pkg.enabled:
            return f"Skill '{name}' 已被禁用。"

        if name in self._resident:
            parts: list[str] = [
                f"Skill '{name}' 已常驻在系统提示词里（「已常驻的 Skill」一节），说明就在上下文中，"
                "不用再加载；直接按说明调用工具。"
            ]
        else:
            parts = [render_skill_text(pkg)]

        if self._enforce:
            # 硬约束模式下，<skill:name>/... 资源只认本轮加载过的 Skill（见 tools/delegation.py）
            from csagent.tools.delegation import note_skill_loaded
            note_skill_loaded(name)

        # allowed-tools 约束；Skill 自己声明的工具天然在允许范围内
        if pkg.allowed_tools:
            allowed = [*pkg.allowed_tools, *(s.name for s in pkg.tools if s.name not in pkg.allowed_tools)]
            tools_str = ", ".join(allowed)
            if self._enforce:
                # 硬约束：写入本轮委托范围，registry.execute() 会拦截越界调用
                from csagent.tools.registry import apply_skill_whitelist
                apply_skill_whitelist(allowed)
                parts.append(
                    f"\n## 本 Skill 限定的工具集（硬约束，越界调用将被拦截）\n{tools_str}"
                )
            else:
                parts.append(f"\n## 本 Skill 建议使用的工具\n{tools_str}")

        logger.info(
            "skill_loaded",
            name=name,
            version=pkg.version,
            source=pkg.source,
            enforce=self._enforce,
            allowed_tools=pkg.allowed_tools,
        )
        return "\n".join(parts)

    def _find_similar(self, name: str, top_k: int = 3) -> list[str]:
        """简单相似匹配：前缀/子串。"""
        packages = self._discovery.scan()
        enabled = [p for p in packages if p.enabled]
        scored: list[tuple[int, str]] = []
        name_lower = name.lower()

        for p in enabled:
            pname = p.name.lower()
            if pname.startswith(name_lower) or name_lower.startswith(pname):
                scored.append((2, p.name))
            elif name_lower in pname or pname in name_lower:
                scored.append((1, p.name))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:top_k]]


class SkillToolPack(ToolPack):
    """Skill 工具包：提供 list_skills 和 load_skill。"""

    pack_name = "skills"
    description = "Skill 索引与加载"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []
        self._discovery: SkillDiscovery | None = None
        self._enforce: bool = False

    def set_discovery(
        self,
        discovery: SkillDiscovery,
        *,
        enforce_allowed_tools: bool = False,
        resident: Iterable[str] = (),
    ) -> None:
        """外部注入 SkillDiscovery 实例。"""
        self._discovery = discovery
        self._enforce = enforce_allowed_tools
        self._tools = [
            ListSkillsTool(discovery),
            LoadSkillTool(discovery, enforce_allowed_tools=enforce_allowed_tools, resident=resident),
        ]

    def initialize(self, config: dict[str, Any]) -> None:
        """ToolPack 接口：由 ToolPackLoader 调用。

        SkillDiscovery 由 main.py 通过 set_discovery 注入，
        此处如果尚未注入则创建默认实例。
        """
        self._enforce = bool(config.get("enforce_allowed_tools", False))
        from csagent.infra.agents_config import load_resident_skills

        resident = load_resident_skills()

        if self._discovery is not None:
            # 已由外部注入，只需更新 enforce 配置
            self._tools = [
                ListSkillsTool(self._discovery),
                LoadSkillTool(self._discovery, enforce_allowed_tools=self._enforce, resident=resident),
            ]
            return

        from csagent.infra.config import PROJECT_ROOT
        from pathlib import Path

        roots_raw = config.get("roots") or ["skills"]
        roots: list[Path] = []
        for r in roots_raw:
            r_str = str(r)
            if r_str.startswith("~"):
                roots.append(Path(r_str).expanduser())
            elif Path(r_str).is_absolute():
                roots.append(Path(r_str))
            else:
                roots.append(PROJECT_ROOT / r_str)

        from csagent.infra.agents_config import load_skill_allowlist

        self._discovery = SkillDiscovery(roots=roots, allowlist=load_skill_allowlist())
        self._tools = [
            ListSkillsTool(self._discovery),
            LoadSkillTool(self._discovery, enforce_allowed_tools=self._enforce, resident=resident),
        ]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)

    def get_discovery(self) -> SkillDiscovery | None:
        """获取内部的 SkillDiscovery 实例。"""
        return self._discovery
