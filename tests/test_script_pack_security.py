"""脚本执行的权限边界：哪些脚本能跑、子进程能看到哪些环境变量。

- ``skill_scripts_only``：对客形态只跑 Skill 自带脚本，会话脚本 / 共享脚本一律拒绝；
- ``env_extra`` 只注入给 Skill 自带脚本，``env_extra_all`` 所有脚本可见；
- 继承模式下名字像密钥的环境变量（KEY / SECRET / TOKEN / PASSWORD…）被剥掉；
- product 形态配置确实关掉了 write_script 并打开了 skill_scripts_only。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from csagent.common.secrets import is_secret_env_name
from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.base import ToolCallError
from csagent.tools.packs.script_pack import ScriptToolPack, _build_scope_env
from csagent.tools.skill_paths import set_skill_discovery_getter

_DUMP_ENV = (
    "import os\n"
    "for name in ('BRIDGE_TOKEN_X', 'DATA_DIR_X', 'FAKE_API_KEY', 'PLAIN_SETTING'):\n"
    "    print(name + '=' + os.environ.get(name, '<absent>'))\n"
)


def _scope() -> CustomerScope:
    return CustomerScope(session_id="web:me", channel="test")


@pytest.fixture
def skill_root(tmp_path: Path):
    root = tmp_path / "skills"
    scripts = root / "demo" / "scripts"
    scripts.mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 用于脚本权限边界测试的 Skill 描述文本。\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (scripts / "dump_env.py").write_text(_DUMP_ENV, encoding="utf-8")
    discovery = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: discovery)
    yield root
    set_skill_discovery_getter(None)


def _pack(tmp_path: Path, **config) -> ScriptToolPack:
    pack = ScriptToolPack()
    pack.initialize({
        "scripts_dir": str(tmp_path / "scripts"),
        "work_dir": str(tmp_path),
        "allowed_extensions": [".py"],
        "shell_enabled": False,
        "timeout_seconds": 30,
        **config,
    })
    return pack


def _tools(pack: ScriptToolPack) -> dict:
    return {t.name: t for t in pack.get_tools()}


@pytest.mark.asyncio
async def test_skill_scripts_only_rejects_session_and_shared_scripts(
    tmp_path: Path, skill_root: Path
) -> None:
    pack = _pack(tmp_path, write_enabled=False, skill_scripts_only=True)
    tools = _tools(pack)
    assert "write_script" not in tools
    run = tools["run_script"]
    assert "只允许运行 Skill 自带脚本" in run.description

    shared = Path(pack.scripts_dir) / "shared.py"
    shared.write_text("print('should-not-run')", encoding="utf-8")
    session_dir = Path(pack.scripts_dir) / "web_me"
    session_dir.mkdir(parents=True)
    (session_dir / "mine.py").write_text("print('should-not-run')", encoding="utf-8")

    for script in ("shared.py", "mine.py", "web_me/mine.py"):
        with pytest.raises(ToolCallError) as exc:  # 拒绝也是失败，审计里记 error
            await run.execute({"script": script}, _scope())
        out = exc.value.text
        assert out.startswith("错误") and "Skill 自带脚本" in out, (script, out)
        assert "should-not-run" not in out
    listing = await run.execute({"action": "list"}, _scope())
    assert "shared.py" not in listing

    ok = await run.execute({"script": "<skill:demo>/scripts/dump_env.py"}, _scope())
    assert "PLAIN_SETTING=" in ok


@pytest.mark.asyncio
async def test_env_extra_reaches_only_skill_scripts(
    tmp_path: Path, skill_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_TOKEN_X", "bridge-credential")
    monkeypatch.setenv("DATA_DIR_X", "/srv/data")
    pack = _pack(
        tmp_path,
        env_extra=["BRIDGE_TOKEN_X"],
        env_extra_all=["DATA_DIR_X"],
    )
    tools = _tools(pack)

    skill_out = await tools["run_script"].execute(
        {"script": "<skill:demo>/scripts/dump_env.py"}, _scope()
    )
    assert "BRIDGE_TOKEN_X=bridge-credential" in skill_out
    assert "DATA_DIR_X=/srv/data" in skill_out

    await tools["write_script"].execute({"path": "dump.py", "content": _DUMP_ENV}, _scope())
    session_out = await tools["run_script"].execute({"script": "dump.py"}, _scope())
    # 模型现写的脚本拿不到 Skill 专用的凭证，只拿得到非敏感的公共变量
    assert "BRIDGE_TOKEN_X=<absent>" in session_out
    assert "DATA_DIR_X=/srv/data" in session_out


@pytest.mark.asyncio
async def test_inherited_secret_env_vars_are_stripped(
    tmp_path: Path, skill_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PLAIN_SETTING", "kept")
    tools = _tools(_pack(tmp_path))
    out = await tools["run_script"].execute(
        {"script": "<skill:demo>/scripts/dump_env.py"}, _scope()
    )
    assert "FAKE_API_KEY=<absent>" in out
    assert "PLAIN_SETTING=kept" in out


def test_scope_env_strips_secret_names_but_keeps_runtime_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("LLM_API_KEY", "WECOM_SECRET", "AGENT_SHARED_SECRET", "ADMIN_ACCOUNTS",
                 "SOME_PASSWORD", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "x" * 20)
    monkeypatch.setenv("PYTHONPATH", "/opt/app/src")
    env = _build_scope_env(_scope(), project_root=tmp_path)
    for name in ("LLM_API_KEY", "WECOM_SECRET", "AGENT_SHARED_SECRET", "ADMIN_ACCOUNTS",
                 "SOME_PASSWORD", "GITHUB_TOKEN"):
        assert name not in env, name
    assert env["PYTHONPATH"] == "/opt/app/src"
    assert "PATH" in {k.upper() for k in env}
    # 白名单模式下显式列出的名字照给
    allowed = _build_scope_env(_scope(), env_passthrough=["LLM_API_KEY"], project_root=tmp_path)
    assert allowed.get("LLM_API_KEY") == "x" * 20


def test_secret_env_name_detection() -> None:
    for name in ("LLM_API_KEY", "wecom_secret", "PRODUCT_PC_BRIDGE_TOKEN", "DB_PASSWORD",
                 "ADMIN_ACCOUNTS", "SSH_PRIVATE_FILE"):
        assert is_secret_env_name(name), name
    for name in ("PATH", "PYTHONPATH", "FUND_DATA_DIR", "PWD", "LANG", "RESEARCH_AGENT_BASE_URL"):
        assert not is_secret_env_name(name), name


def test_product_profile_locks_down_script_execution() -> None:
    profile = Path(__file__).resolve().parents[1] / "configs" / "profiles" / "product" / "profile.yaml"
    overlay = yaml.safe_load(profile.read_text(encoding="utf-8"))
    config = overlay["tool_pack_overrides"]["script_executor"]["config"]
    assert config["write_enabled"] is False
    assert config["skill_scripts_only"] is True
