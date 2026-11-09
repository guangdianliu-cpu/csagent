"""本轮委托范围：Skill 收窄的工具白名单跨工具批次生效，未加载 Skill 的资源读不到。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from csagent.skills.discovery import SkillDiscovery
from csagent.tools.delegation import (
    begin_delegation_scope,
    delegation_scope_var,
    note_skill_loaded,
    restrict_tools,
)
from csagent.tools.registry import clear_skill_whitelist, session_tool_whitelist
from csagent.tools.skill_paths import resolve_skill_resource, set_skill_discovery_getter


@pytest.mark.asyncio
async def test_whitelist_survives_gather_and_skill_resource_is_gated(tmp_path: Path):
    """子任务里收窄的白名单，gather 之后仍能看见；未加载的 Skill 资源读不到。"""
    token = begin_delegation_scope()
    try:
        async def load(names: list[str]) -> None:
            restrict_tools(names)

        await asyncio.gather(load(["read_file"]), load(["run_script"]))
        assert session_tool_whitelist() == {"read_file", "run_script"}
    finally:
        delegation_scope_var.reset(token)
        clear_skill_whitelist()

    root = tmp_path / "skills"
    for name in ("loaded-one", "other-one"):
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 描述足够长以便通过校验，不少于二十个字符。\n---\n\n正文\n",
            encoding="utf-8",
        )
        (skill / "note.txt").write_text("secret", encoding="utf-8")
    set_skill_discovery_getter(lambda: SkillDiscovery(roots=[root]))
    scope_token = begin_delegation_scope()
    try:
        note_skill_loaded("loaded-one")
        allowed = resolve_skill_resource("<skill:loaded-one>/note.txt")
        assert isinstance(allowed, Path)
        blocked = resolve_skill_resource("<skill:other-one>/note.txt")
        assert isinstance(blocked, str)
        assert "未加载" in blocked
    finally:
        delegation_scope_var.reset(scope_token)
        set_skill_discovery_getter(lambda: None)
