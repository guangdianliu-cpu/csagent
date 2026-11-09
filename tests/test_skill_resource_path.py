"""<skill:name>/rel_path 解析：skill_paths.py 单测 + read_file/run_script 端到端验证。

之前这条路径解析长期是死代码：run_script 完全没实现，read_file 虽有正则
但从未接上真正的 SkillDiscovery（main.py 建好 discovery 后没调
set_skill_discovery_getter）。本文件第一次真正验证 wiring 生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.base import ToolCallError
from csagent.tools.packs.script_pack import ScriptTool
from csagent.tools.packs.workspace_files_pack import ReadFileTool
from csagent.tools.skill_paths import (
    is_skill_path,
    resolve_skill_resource,
    set_skill_discovery_getter,
)


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


def _write_skill(root: Path, name: str, description: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    # SKILL.md 校验要求 description >= 20 字符，测试用简写描述统一补齐
    padded = description if len(description) >= 20 else description + "（测试用途，凑够校验所需长度）"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {padded}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture(autouse=True)
def _reset_discovery_getter():
    """set_skill_discovery_getter 是模块级全局，测试后必须还原，避免污染其他用例。"""
    yield
    set_skill_discovery_getter(None)


# ------------------------------------------------------------------
# resolve_skill_resource() / is_skill_path() 直接单测
# ------------------------------------------------------------------


def test_is_skill_path_matches_prefix():
    assert is_skill_path("<skill:example-skill>/scripts/build_report.py")
    assert is_skill_path("<skill:fund-research-db>")
    assert is_skill_path("<skill:fund-research-db>/")
    assert not is_skill_path("scripts/tools/demo.py")


def test_resolve_skill_root_path(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于 Skill 根目录 list 的描述文本足够长。")
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    result = resolve_skill_resource("<skill:demo>")
    assert isinstance(result, Path)
    assert result.resolve() == skill_dir.resolve()


def test_resolve_skill_resource_without_wiring_returns_error():
    set_skill_discovery_getter(None)
    result = resolve_skill_resource("<skill:foo>/scripts/x.py")
    assert isinstance(result, str)
    assert "未初始化" in result


def test_resolve_skill_resource_unknown_skill(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    result = resolve_skill_resource("<skill:nope>/scripts/x.py")
    assert isinstance(result, str)
    assert "未找到" in result


def test_resolve_skill_resource_blocks_disabled(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于禁用状态测试的描述文本，需要足够长才能通过校验。")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 用于禁用状态测试的描述文本，需要足够长才能通过校验。\n"
        "enabled: false\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "hello.py").write_text("print('hi')\n", encoding="utf-8")

    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    result = resolve_skill_resource("<skill:demo>/scripts/hello.py")
    assert isinstance(result, str)
    assert "禁用" in result


def test_resolve_skill_resource_happy_path(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于路径解析测试的描述文本，需要足够长才能通过校验。")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "hello.py").write_text("print('hi')\n", encoding="utf-8")

    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    resolved = resolve_skill_resource("<skill:demo>/scripts/hello.py")
    assert isinstance(resolved, Path)
    assert resolved == (skill_dir / "scripts" / "hello.py").resolve()


def test_resolve_skill_resource_blocks_escape(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "用于路径解析测试的描述文本。")
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    result = resolve_skill_resource("<skill:demo>/../../secret.txt")
    assert isinstance(result, str)
    assert "越出" in result


# ------------------------------------------------------------------
# read_file(path="<skill:name>/...") 端到端
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_resolves_skill_path(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于 read_file 测试的描述文本。")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "doc.md").write_text("# 参考文档\n内容。", encoding="utf-8")

    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    tool = ReadFileTool(root=tmp_path / "workspace")
    result = await tool.execute({"path": "<skill:demo>/references/doc.md"}, _scope())
    assert "参考文档" in result


# ------------------------------------------------------------------
# run_script(script="<skill:name>/scripts/x.py") 端到端
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_script_executes_skill_script(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于 run_script 测试的描述文本。")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "hello.py").write_text(
        "import sys\nprint('skill-script-ok ' + ' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    tool = ScriptTool(scripts_dir=tmp_path / "unused", allowed_extensions=[".py"], timeout_seconds=10)
    out = await tool.execute(
        {"script": "<skill:demo>/scripts/hello.py", "args": ["a", "b"]}, _scope()
    )
    assert "skill-script-ok a b" in out


@pytest.mark.asyncio
async def test_run_script_skill_path_missing_file(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "demo", "用于 run_script 缺失文件测试的描述文本。")
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)

    tool = ScriptTool(scripts_dir=tmp_path / "unused", allowed_extensions=[".py"], timeout_seconds=10)
    with pytest.raises(ToolCallError) as exc:
        await tool.execute({"script": "<skill:demo>/scripts/missing.py"}, _scope())
    assert "不存在" in exc.value.text


@pytest.mark.asyncio
async def test_run_script_env_passthrough_restricts_secrets(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, "demo", "用于 env_passthrough 测试的描述文本。")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "dump_env.py").write_text(
        "import os\nprint('SECRET=' + os.environ.get('FAKE_SECRET', '<absent>'))\n",
        encoding="utf-8",
    )
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)
    monkeypatch.setenv("FAKE_SECRET", "leak-me-not")

    tool = ScriptTool(
        scripts_dir=tmp_path / "unused",
        allowed_extensions=[".py"],
        timeout_seconds=10,
        env_passthrough=["SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC"],
    )
    out = await tool.execute({"script": "<skill:demo>/scripts/dump_env.py"}, _scope())
    assert "SECRET=<absent>" in out
