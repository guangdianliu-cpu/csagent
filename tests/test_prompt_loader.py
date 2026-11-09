"""外置提示词加载测试（单 Agent，唯一模板 agent.md）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from csagent.agent import Agent
from csagent.prompts.capability_notes import format_capability_notes
from csagent.prompts.loader import PROMPTS_DIR, render_prompt, reload_prompts
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.base import BaseTool
from csagent.tools.registry import ToolRegistry


def test_every_profile_has_agent_template():
    """系统提示词按形态放在 configs/profiles/<name>/agent.md。"""
    profiles = PROMPTS_DIR.parent / "profiles"
    dirs = [d for d in profiles.iterdir() if d.is_dir()]
    assert dirs
    for d in dirs:
        assert (d / "agent.md").is_file(), d
        assert (d / "profile.yaml").is_file(), d


def test_render_agent_prompt_replaces_placeholders():
    reload_prompts()
    text = render_prompt(
        "agent",
        capability_notes="- write_file：默认写入会话目录",
    )
    assert "行为准则" in text
    # 系统收口工具已下线：prompt 不得再提 __final_answer__/__escalate__
    assert "__final_answer__" not in text
    assert "__escalate__" not in text
    assert "write_file" in text


def test_capability_notes_injected_when_tool_mounted():
    notes = format_capability_notes(["send_file"])
    assert "send_file" in notes
    assert "明确要求" in notes
    assert "禁止因历史" in notes
    assert format_capability_notes(["product_query"]) == ""

    class _Send(BaseTool):
        name = "send_file"
        description = "send"

        async def execute(self, arguments, scope):  # noqa: ANN001
            return ""

    agent = Agent(
        react_loop=MagicMock(),
        tool_registry=ToolRegistry.from_tools_and_agent([_Send()], "agent"),
        short_term_memory=MagicMock(),
    )
    prompt = agent.build_system_prompt()
    assert "send_file" in prompt


def test_resident_skill_text_goes_to_the_end_of_the_prompt(tmp_path):
    skill = tmp_path / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 常驻测试用的 Skill，描述凑够长度用于通过校验\n---\n\n# 常驻正文\n",
        encoding="utf-8",
    )
    agent = Agent(
        react_loop=MagicMock(),
        tool_registry=ToolRegistry.from_tools_and_agent([], "agent"),
        short_term_memory=MagicMock(),
        skill_discovery=SkillDiscovery(roots=[tmp_path / "skills"]),
        resident_skills=("demo-skill",),
    )
    prompt = agent.build_system_prompt()
    assert "demo-skill（已常驻" in prompt  # 索引里标明不用 load_skill
    tail = prompt[prompt.index("## 已常驻的 Skill：demo-skill"):]
    assert "# 常驻正文" in tail
    assert "行为准则" not in tail  # 身份、路由与边界规则都在常驻正文之前


def test_capability_notes_run_script_mentions_matplotlib():
    # 画图要自己写脚本：只有挂了 write_script 的部署才给这条约定
    notes = format_capability_notes(["run_script", "write_script"])
    assert "matplotlib" in notes
    assert 'matplotlib.use("Agg")' in notes


def test_capability_notes_without_write_script_only_offer_skill_scripts():
    """没挂 write_script（对客形态）时不能再把模型引向自写脚本、画图、读外置文件。"""
    notes = format_capability_notes(["run_script", "read_artifact"])
    assert "<skill:name>/scripts/x.py" in notes
    assert "不能自写脚本" in notes
    assert "matplotlib" not in notes
    assert "直接 run_script 读占位" not in notes
