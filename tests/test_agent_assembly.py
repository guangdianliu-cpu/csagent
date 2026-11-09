"""端到端装配测试：唯一配置（configs/agent.yaml）能正常装配，通用能力与基金查询能力同时挂载。

不再有"两个模式互不泄漏"这回事——只有一个 Agent，这里验证的是它确实按 agent.yaml 的
tool_packs 列表装起来了，且渲染出的 system prompt 没有遗留任何未替换的占位符。
"""

from __future__ import annotations

import pytest

from csagent.infra.config import get_settings
from csagent.main import _build_app_components


@pytest.mark.asyncio
async def test_agent_assembly_mounts_configured_tool_packs():
    get_settings.cache_clear()
    settings = get_settings()
    components = await _build_app_components(settings)
    try:
        agent = components["main_agent"]
        tools = {
            t.name for t in agent._tool_registry.list_tools_for_agent(agent.agent_name)
        }

        assert agent.agent_name == "agent"
        assert agent.domain == "agent"

        # 通用能力和基金查询能力现在挂在同一个 Agent 上
        assert "write_file" in tools
        assert "html_to_pdf" in tools
        assert "read_artifact" in tools
        assert "run_script" in tools
        # run_shell 已关闭（shell_enabled: false），不应出现在挂载清单里
        assert "run_shell" not in tools
        assert "set_reply_style" in tools
        assert "list_skills" in tools
        # §8 旋钮已按建议值写入 YAML，装配时必须读到同一份，不能停留在旧默认。
        assert settings.history_window == 24
        assert settings.summary_max_tokens == 4000
        assert settings.context_token_high_ratio == 0.50
        assert settings.history_summary_max_chars == 16000
        assert settings.workspace_cleanup_ttl_days == 0
        assert agent._context_token_high_ratio == 0.50
    finally:
        await components["tool_pack_loader"].shutdown_all()
        await components["llm_client"].close()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_agent_prompt_has_no_leftover_placeholders():
    get_settings.cache_clear()
    components = await _build_app_components(get_settings())
    try:
        prompt = components["main_agent"].build_system_prompt()
        assert "{{" not in prompt  # 没有未渲染的占位符（如残留的 {{share_sibling_mode}}）
        # 系统收口工具已下线：prompt 不得再提 __final_answer__/__escalate__，收口走纯文本
        assert "__final_answer__" not in prompt
        assert "__escalate__" not in prompt
        # Skill 索引里能看到当前形态白名单里的 Skill（形态由 AGENT_PROFILE 决定，不写死名字）
        enabled = [pkg.name for pkg in components["skill_discovery"].scan() if pkg.enabled]
        assert enabled
        for name in enabled:
            assert name in prompt
    finally:
        await components["tool_pack_loader"].shutdown_all()
        await components["llm_client"].close()
        get_settings.cache_clear()
