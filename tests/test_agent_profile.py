"""部署形态（AGENT_PROFILE）：只靠一个变量切换，不改任何文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.infra import config as cfg
from csagent.prompts import loader
from csagent.skills.discovery import SkillDiscovery


def _write_skill(root: Path, name: str, enabled: bool) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 这是一个用于测试形态白名单的示例技能说明\nenabled: {str(enabled).lower()}\n---\n正文\n",
        encoding="utf-8",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configs = tmp_path / "configs"
    (configs / "prompts").mkdir(parents=True)
    (configs / "agent.yaml").write_text("default_profile: alpha\ntool_packs: [a]\n", encoding="utf-8")
    for name, skills in (("alpha", ["s1"]), ("beta", ["s2"])):
        d = configs / "profiles" / name
        d.mkdir(parents=True)
        (d / "profile.yaml").write_text(
            f"skills: {skills}\nagent:\n  tool_packs: [{name}_pack]\n", encoding="utf-8"
        )
        (d / "agent.md").write_text(f"我是 {name}", encoding="utf-8")
    (configs / "prompts" / "compactor.md").write_text("通用压缩", encoding="utf-8")
    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "CONFIGS_DIR", configs)
    monkeypatch.setattr(cfg, "PROFILES_DIR", configs / "profiles")
    monkeypatch.setattr(loader, "PROMPTS_DIR", configs / "prompts")
    monkeypatch.setattr("csagent.infra.agents_config.PROFILES_DIR", configs / "profiles")
    monkeypatch.setattr("csagent.infra.agents_config.CONFIGS_DIR", configs)
    monkeypatch.delenv("AGENT_PROFILE", raising=False)
    loader.reload_prompts()
    yield tmp_path
    loader.reload_prompts()


def test_default_profile_from_agent_yaml(project: Path):
    assert cfg.active_profile() == "alpha"
    assert loader.render_prompt("agent").strip() == "我是 alpha"


def test_env_var_selects_profile(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_PROFILE", "beta")
    assert cfg.active_profile() == "beta"
    assert loader.render_prompt("agent").strip() == "我是 beta"
    from csagent.infra.agents_config import load_skill_allowlist, load_tool_pack_names

    assert load_skill_allowlist() == frozenset({"s2"})
    assert load_tool_pack_names() == ("beta_pack",)


def test_dotenv_selects_profile(project: Path):
    (project / ".env").write_text("AGENT_PROFILE=beta\n", encoding="utf-8")
    assert cfg.active_profile() == "beta"


def test_non_profile_prompt_falls_back_to_prompts_dir(project: Path):
    assert loader.render_prompt("compactor").strip() == "通用压缩"


def test_env_files_layer_profile_file(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_PROFILE", "beta")
    assert [p.name for p in cfg.env_files()] == [".env", ".env.beta"]


def test_profile_env_file_overrides_base(project: Path, monkeypatch: pytest.MonkeyPatch):
    (project / ".env").write_text("APP_PORT=8000\nWECOM_BOT_ID=base\n", encoding="utf-8")
    (project / ".env.beta").write_text("APP_PORT=8001\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROFILE", "beta")
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    settings = cfg.Settings(_env_file=cfg.env_files())
    assert settings.app_port == 8001
    assert settings.wecom_bot_id == "base"


def test_resident_skills_come_from_the_profile(project: Path, monkeypatch: pytest.MonkeyPatch):
    from csagent.infra.agents_config import load_resident_skills

    assert load_resident_skills() == ()  # 没写这个键就没有常驻
    (project / "configs" / "profiles" / "beta" / "profile.yaml").write_text(
        "skills: [s2]\nresident_skills: [s2]\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_PROFILE", "beta")
    assert load_resident_skills() == ("s2",)


def test_real_resident_skills_are_in_the_profile_allowlist():
    """常驻的只能是本形态白名单里的 Skill，否则启动报 resident_skill_unavailable。"""
    import yaml

    profiles = Path(__file__).resolve().parents[1] / "configs" / "profiles"
    for path in profiles.glob("*/profile.yaml"):
        overlay = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert set(overlay.get("resident_skills") or []) <= set(overlay.get("skills") or []), path


def test_profile_name_cannot_escape(project: Path):
    assert cfg.profile_dir("../etc") is None
    assert cfg.profile_dir(".hidden") is None


def test_skill_allowlist_overrides_skill_md(tmp_path: Path):
    _write_skill(tmp_path, "s1", enabled=False)
    _write_skill(tmp_path, "s2", enabled=True)
    discovery = SkillDiscovery([tmp_path], allowlist=frozenset({"s1"}))
    enabled = {p.name for p in discovery.scan() if p.enabled}
    assert enabled == {"s1"}
    # 不给白名单时沿用 SKILL.md
    legacy = SkillDiscovery([tmp_path])
    assert {p.name for p in legacy.scan() if p.enabled} == {"s2"}


def test_real_profiles_have_disjoint_skills():
    """仓库里的两套形态各自只看得到自己的业务 Skill。"""
    import yaml

    profiles = Path(__file__).resolve().parents[1] / "configs" / "profiles"
    research = yaml.safe_load((profiles / "research" / "profile.yaml").read_text(encoding="utf-8"))
    product = yaml.safe_load((profiles / "product" / "profile.yaml").read_text(encoding="utf-8"))
    assert "fund-research-db" in research["skills"]
    assert "fund-product-query" in product["skills"]
    assert not set(research["skills"]) & set(product["skills"])
