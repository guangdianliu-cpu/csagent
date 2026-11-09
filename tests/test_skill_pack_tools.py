"""test_skill_pack_tools.py — list_skills / load_skill 工具测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.packs.skill_pack import ListSkillsTool, LoadSkillTool, SkillToolPack, render_resident_skills


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


def _write_skill(root: Path, name: str, description: str, body: str = "Body.",
                 resources: dict[str, str] | None = None,
                 allowed_tools: list[str] | None = None) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}", f"description: {description}"]
    if allowed_tools:
        fm_lines.append(f"allowed-tools: [{', '.join(allowed_tools)}]")
    fm = "\n".join(fm_lines)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8"
    )
    if resources:
        for rel, content in resources.items():
            p = skill_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return skill_dir


# ---------- ListSkillsTool ----------

@pytest.mark.asyncio
async def test_list_skills_all(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "alpha", "Alpha 的详细描述信息足够长用于通过校验。")
    _write_skill(root, "beta", "Beta 的详细描述信息也足够长用于通过校验。")

    discovery = SkillDiscovery(roots=[root])
    tool = ListSkillsTool(discovery)
    result = await tool.execute({}, _scope())

    assert "alpha" in result
    assert "beta" in result


@pytest.mark.asyncio
async def test_list_skills_filter(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "example-skill", "生成与美化示例报表，供 list_skills 过滤测试使用。")
    _write_skill(root, "code-review", "对代码变更做结构化评审的测试描述。")

    discovery = SkillDiscovery(roots=[root])
    tool = ListSkillsTool(discovery)
    result = await tool.execute({"query": "example"}, _scope())

    assert "example-skill" in result
    assert "code-review" not in result


@pytest.mark.asyncio
async def test_list_skills_empty(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    discovery = SkillDiscovery(roots=[root])
    tool = ListSkillsTool(discovery)
    result = await tool.execute({}, _scope())
    assert "没有" in result or "当前" in result


# ---------- LoadSkillTool ----------

@pytest.mark.asyncio
async def test_load_skill_success(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root, "my-tool", "一个测试用的 Skill 描述信息足够长。",
        body="# 操作指南\n\n执行步骤...",
        resources={"scripts/run.py": "pass", "references/doc.md": "# Doc"},
        allowed_tools=["read_file", "run_script"],
    )

    discovery = SkillDiscovery(roots=[root])
    tool = LoadSkillTool(discovery)
    result = await tool.execute({"name": "my-tool"}, _scope())

    # 包含正文
    assert "操作指南" in result
    # 包含资源清单
    assert "scripts/run.py" in result
    assert "<skill:my-tool>" in result
    # 包含工具建议
    assert "read_file" in result


# ---------- 常驻 Skill ----------

@pytest.mark.asyncio
async def test_load_skill_for_resident_skill_only_points_back(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "my-tool", "一个测试用的 Skill 描述信息足够长。", body="# 操作指南\n\n执行步骤...")

    discovery = SkillDiscovery(roots=[root])
    result = await LoadSkillTool(discovery, resident=["my-tool"]).execute({"name": "my-tool"}, _scope())

    assert "已常驻" in result
    assert "操作指南" not in result  # 正文已在系统提示词里，不再塞第二遍


def test_resident_block_has_full_text_and_index_marks_it(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root, "my-tool", "一个测试用的 Skill 描述信息足够长。",
        body="# 操作指南\n\n执行步骤...", resources={"scripts/run.py": "pass"},
    )
    _write_skill(root, "other", "另一个测试用的 Skill 描述信息足够长。")
    discovery = SkillDiscovery(roots=[root])

    block = render_resident_skills(discovery, ["my-tool", "missing"])
    assert block.startswith("## 已常驻的 Skill：my-tool")
    assert "操作指南" in block
    assert '<skill:my-tool>/scripts/run.py' in block
    assert "missing" not in block  # 不存在的名字跳过（启动时 main 已报错）

    lines = discovery.render_index(resident=["my-tool"]).splitlines()
    assert "已常驻" in next(line for line in lines if line.startswith("- my-tool"))
    assert "已常驻" not in next(line for line in lines if line.startswith("- other"))


@pytest.mark.asyncio
async def test_load_skill_not_found(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "existing", "一个已存在的 Skill 描述信息足够长。")

    discovery = SkillDiscovery(roots=[root])
    tool = LoadSkillTool(discovery)
    result = await tool.execute({"name": "nonexistent"}, _scope())
    assert "未找到" in result


@pytest.mark.asyncio
async def test_load_skill_missing_name(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    discovery = SkillDiscovery(roots=[root])
    tool = LoadSkillTool(discovery)
    result = await tool.execute({}, _scope())
    assert "错误" in result


# ---------- SkillToolPack ----------

def test_skill_tool_pack_initialize(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "pack-test", "用于测试 ToolPack 初始化的描述。")

    pack = SkillToolPack()
    discovery = SkillDiscovery(roots=[root])
    pack.set_discovery(discovery)

    tools = pack.get_tools()
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert names == {"list_skills", "load_skill"}


# ---------- Skill 路径写保护 ----------

@pytest.mark.asyncio
async def test_write_file_rejects_skill_path():
    from csagent.tools.packs.workspace_files_pack import is_skill_path
    assert is_skill_path("<skill:example-skill>/scripts/run.py")
    assert not is_skill_path("data/file.txt")
