"""test_skill_permissions.py — Skill 权限与白名单机制测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.packs.skill_pack import LoadSkillTool, SkillToolPack
from csagent.tools.registry import (
    ToolRegistry,
    apply_skill_whitelist,
    clear_skill_whitelist,
    session_tool_whitelist,
)


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


def _write_skill(
    root: Path,
    name: str,
    description: str,
    body: str = "Body content here.",
    allowed_tools: list[str] | None = None,
) -> Path:
    """辅助函数：在指定根目录下创建一个 Skill 包。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}", f"description: {description}"]
    if allowed_tools:
        fm_lines.append(f"allowed-tools: [{', '.join(allowed_tools)}]")
    fm = "\n".join(fm_lines)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8"
    )
    return skill_dir


# ---------- apply / clear 白名单 ----------

class TestWhitelistManagement:
    """会话级白名单设置与清除。"""

    def setup_method(self):
        clear_skill_whitelist()

    def teardown_method(self):
        clear_skill_whitelist()

    def test_apply_creates_whitelist(self):
        assert session_tool_whitelist() is None
        apply_skill_whitelist(["read_file", "run_script"])
        wl = session_tool_whitelist()
        assert wl == {"read_file", "run_script"}

    def test_apply_union_mode(self):
        """多次 apply 取并集，不互相覆盖。"""
        apply_skill_whitelist(["read_file"])
        apply_skill_whitelist(["write_file", "run_shell"])
        wl = session_tool_whitelist()
        assert wl == {"read_file", "write_file", "run_shell"}

    def test_clear_resets_to_none(self):
        apply_skill_whitelist(["read_file"])
        clear_skill_whitelist()
        assert session_tool_whitelist() is None


# ---------- ToolRegistry 白名单拦截 ----------

class TestRegistryWhitelistEnforcement:
    """ToolRegistry.execute() 的白名单判定。"""

    def setup_method(self):
        clear_skill_whitelist()

    def teardown_method(self):
        clear_skill_whitelist()

    @pytest.mark.asyncio
    async def test_no_whitelist_allows_all(self):
        """无白名单时所有工具正常执行。"""
        from csagent.tools.base import BaseTool

        class DummyTool(BaseTool):
            name = "dummy"
            description = "test"
            async def execute(self, arguments, scope):
                return "ok"

        registry = ToolRegistry(
            tools=[DummyTool()],
            permission_matrix={"agent": ["dummy"]},
        )
        result = await registry.execute("dummy", {}, _scope(), "agent")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_whitelist_blocks_unlisted_tool(self):
        """白名单启用后，不在列表内的工具被拒绝。"""
        from csagent.tools.base import BaseTool

        class AllowedTool(BaseTool):
            name = "allowed"
            description = "test"
            async def execute(self, arguments, scope):
                return "allowed_ok"

        class BlockedTool(BaseTool):
            name = "blocked"
            description = "test"
            async def execute(self, arguments, scope):
                return "should_not_reach"

        registry = ToolRegistry(
            tools=[AllowedTool(), BlockedTool()],
            permission_matrix={"agent": ["allowed", "blocked"]},
        )

        apply_skill_whitelist(["allowed"])

        # 允许的工具正常
        result = await registry.execute("allowed", {}, _scope(), "agent")
        assert result == "allowed_ok"

        # 不在白名单的工具被拦截
        result = await registry.execute("blocked", {}, _scope(), "agent")
        assert "权限错误" in result
        assert "blocked" in result

    @pytest.mark.asyncio
    async def test_always_allowed_bypasses_whitelist(self):
        """_ALWAYS_ALLOWED 中的工具不受白名单限制。"""
        from csagent.tools.base import BaseTool

        class ListSkills(BaseTool):
            name = "list_skills"
            description = "test"
            async def execute(self, arguments, scope):
                return "skills_list"

        registry = ToolRegistry(
            tools=[ListSkills()],
            permission_matrix={"agent": ["list_skills"]},
        )

        apply_skill_whitelist(["read_file"])  # list_skills 不在白名单中

        result = await registry.execute("list_skills", {}, _scope(), "agent")
        assert result == "skills_list"


# ---------- LoadSkillTool 硬约束模式 ----------

class TestLoadSkillEnforcement:
    """load_skill 在 enforce=True 时自动应用白名单。"""

    def setup_method(self):
        clear_skill_whitelist()

    def teardown_method(self):
        clear_skill_whitelist()

    @pytest.mark.asyncio
    async def test_enforce_mode_sets_whitelist(self, tmp_path):
        """enforce=True 时，加载带 allowed-tools 的 Skill 会设置白名单。"""
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(
            root, "restricted-skill",
            "一个限定工具集的 Skill 描述足够长。",
            allowed_tools=["read_file", "run_script"],
        )

        discovery = SkillDiscovery(roots=[root])
        tool = LoadSkillTool(discovery, enforce_allowed_tools=True)
        result = await tool.execute({"name": "restricted-skill"}, _scope())

        # 返回体应包含硬约束提示
        assert "硬约束" in result
        assert "read_file" in result

        # 白名单已被设置
        wl = session_tool_whitelist()
        assert wl is not None
        assert "read_file" in wl
        assert "run_script" in wl

    @pytest.mark.asyncio
    async def test_soft_mode_no_whitelist(self, tmp_path):
        """enforce=False 时，加载不设置白名单。"""
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(
            root, "soft-skill",
            "一个软约束 Skill 的描述足够长用于通过。",
            allowed_tools=["read_file"],
        )

        discovery = SkillDiscovery(roots=[root])
        tool = LoadSkillTool(discovery, enforce_allowed_tools=False)
        result = await tool.execute({"name": "soft-skill"}, _scope())

        # 软约束提示
        assert "建议" in result
        # 白名单未被设置
        assert session_tool_whitelist() is None

    @pytest.mark.asyncio
    async def test_no_allowed_tools_no_whitelist(self, tmp_path):
        """Skill 没有 allowed-tools 时，即使 enforce=True 也不设白名单。"""
        root = tmp_path / "skills"
        root.mkdir()
        _write_skill(
            root, "free-skill",
            "一个没有工具限制的 Skill 描述足够长。",
            allowed_tools=None,
        )

        discovery = SkillDiscovery(roots=[root])
        tool = LoadSkillTool(discovery, enforce_allowed_tools=True)
        await tool.execute({"name": "free-skill"}, _scope())

        assert session_tool_whitelist() is None


# ---------- SkillToolPack enforce 配置传递 ----------

def test_skill_tool_pack_enforce_config(tmp_path):
    """SkillToolPack.initialize() 正确传递 enforce_allowed_tools 配置。"""
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "cfg-test", "用于测试配置传递的 Skill 描述信息。")

    pack = SkillToolPack()
    discovery = SkillDiscovery(roots=[root])
    pack.set_discovery(discovery, enforce_allowed_tools=True)

    tools = pack.get_tools()
    load_tool = next(t for t in tools if t.name == "load_skill")
    assert load_tool._enforce is True
