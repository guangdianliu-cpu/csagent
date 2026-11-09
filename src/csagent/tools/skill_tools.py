"""Skill 声明的原生工具（``skills/<name>/tools.yaml``）。

Skill 的业务能力以前只能经通用的 run_script 调用：参数是字符串数组，结构化查询只能把
JSON 序列化成字符串塞进去，模型要写两层转义，参数也没有 schema 可校验（见 2026-10-22
审计：一天 12 次脚本失败，其中 6 次是查询接口用错）。

Skill 在 tools.yaml 里声明工具名、说明和 JSON Schema 后，内核把它注册成普通的 function
calling 工具。调用时把参数 JSON 原样写进脚本 stdin，脚本按 Skill 脚本的口径运行（环境变量、
超时、输出上限都与 run_script 跑 Skill 脚本一致），退出码非 0 记为调用失败。内核不认识任何
具体业务：加一个工具只改 Skill 目录。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.skills.package import SkillToolSpec
from csagent.tools.base import BaseTool, ToolCallError, ToolPack
from csagent.tools.packs.script_pack import ScriptTool

logger = structlog.get_logger(__name__)


class SkillCommandTool(BaseTool):
    """把 Skill 的一个脚本包装成原生工具：参数 JSON 走 stdin，stdout 就是结果。"""

    def __init__(self, skill: str, spec: SkillToolSpec, runner: ScriptTool) -> None:
        self.name = spec.name
        self.description = spec.description
        self.skill = skill
        self._spec = spec
        self._runner = runner

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self._spec.parameters

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        run = await self._runner.run_skill_command(
            f"<skill:{self.skill}>/{self._spec.script}",
            self._spec.argv,
            scope,
            stdin_text=json.dumps(arguments or {}, ensure_ascii=False),
        )
        if run.ok:
            return run.text
        # 脚本自己给了结构化的失败说明（{"ok": false, "error": …}）就只回它，不带「脚本执行失败」外壳
        raise ToolCallError(run.stdout.strip() if _is_json_object(run.stdout) else run.text)


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def build_skill_tools(
    discovery: SkillDiscovery | None,
    packs: dict[str, ToolPack],
    *,
    taken: set[str],
) -> list[BaseTool]:
    """为已启用 Skill 声明的工具建实例。

    :param packs: 已加载的工具包；Skill 工具借 script_executor 的 run_script 跑脚本
    :param taken: 已有的工具名；重名的 Skill 工具跳过并报错，不覆盖内置工具
    """
    if discovery is None:
        return []
    packages = sorted((p for p in discovery.scan() if p.enabled and p.tools), key=lambda p: p.name)
    if not packages:
        return []
    runner = _find_script_runner(packs)
    if runner is None:
        logger.error(
            "skill_tools_need_script_executor",
            skills=[p.name for p in packages],
            hint="形态的 tool_packs 里要有 script_executor，Skill 声明的工具才能运行",
        )
        return []

    names = set(taken)
    tools: list[BaseTool] = []
    for pkg in packages:
        for spec in pkg.tools:
            if spec.name in names:
                logger.error("skill_tool_name_conflict", skill=pkg.name, tool=spec.name)
                continue
            names.add(spec.name)
            tools.append(SkillCommandTool(pkg.name, spec, runner))
    logger.info("skill_tools_ready", tools=[t.name for t in tools])
    return tools


def _find_script_runner(packs: dict[str, ToolPack]) -> ScriptTool | None:
    for pack in packs.values():
        for tool in pack.get_tools():
            if isinstance(tool, ScriptTool):
                return tool
    return None
