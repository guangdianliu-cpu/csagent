"""Skill 声明的原生工具：tools.yaml 解析、注册、参数经 stdin 传给脚本、失败标记与 python-path。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csagent.react.models import CustomerScope
from csagent.skills.discovery import SkillDiscovery
from csagent.tools.base import ToolCallError
from csagent.tools.delegation import delegation_scope_var
from csagent.tools.packs.script_pack import ScriptTool, ScriptToolPack, WriteScriptTool
from csagent.tools.packs.skill_pack import LoadSkillTool
from csagent.tools.registry import ToolRegistry
from csagent.tools.skill_paths import set_skill_discovery_getter
from csagent.tools.skill_tools import SkillCommandTool, build_skill_tools

_REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills"

_ECHO = """\
import json, sys
payload = json.loads(sys.stdin.read() or "{}")
if payload.get("fail") == "json":
    print(json.dumps({"ok": False, "error": {"code": "BAD", "message": "坏参数"}}, ensure_ascii=False))
    sys.exit(1)
if payload.get("fail") == "crash":
    raise RuntimeError("boom")
print(json.dumps({"ok": True, "argv": sys.argv[1:], "got": payload}, ensure_ascii=False))
"""

# 只有第一个是合法的，其余各错一处，应被逐个跳过
_MANIFEST = """\
tools:
  - name: demo_echo
    script: scripts/echo.py
    argv: [--mode, echo]
    description: 回显参数
    parameters:
      type: object
      properties:
        q: {type: string}
  - name: Bad-Name
    script: scripts/echo.py
    description: 名字不合法
    parameters: {type: object}
  - name: demo_outside
    script: ../evil.py
    description: 脚本不在 scripts/ 下
    parameters: {type: object}
  - name: demo_missing
    script: scripts/nope.py
    description: 脚本不存在
    parameters: {type: object}
  - name: demo_params
    script: scripts/echo.py
    description: parameters 不是 object
    parameters: {type: array}
  - name: demo_echo
    script: scripts/echo.py
    description: 重名
    parameters: {type: object}
"""


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


def _write_skill(root: Path, *, manifest: str = _MANIFEST, frontmatter: str = "") -> Path:
    skill = root / "demo-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "echo.py").write_text(_ECHO, encoding="utf-8")
    (skill / "scripts" / "helpermod.py").write_text("GREETING = 'hello-from-skill'\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示 Skill 声明的原生工具（测试用途，描述凑够长度）\n"
        f"{frontmatter}---\n\nBody.\n",
        encoding="utf-8",
    )
    if manifest:
        (skill / "tools.yaml").write_text(manifest, encoding="utf-8")
    return skill


@pytest.fixture(autouse=True)
def _isolate_globals():
    """Skill discovery getter 与委托范围都是模块级全局，用例之间不能串。"""
    token = delegation_scope_var.set(None)
    yield
    delegation_scope_var.reset(token)
    set_skill_discovery_getter(None)


@pytest.fixture()
def discovery(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, frontmatter="python-path: [scripts, ../outside, missing]\n")
    found = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: found)
    return found


@pytest.fixture()
def runner(tmp_path):
    return ScriptTool(scripts_dir=tmp_path / "session-scripts", allowed_extensions=[".py"], timeout_seconds=20)


def _tool(discovery, runner) -> SkillCommandTool:
    return SkillCommandTool("demo-skill", discovery.get("demo-skill").tools[0], runner)


def _script_pack(tmp_path) -> ScriptToolPack:
    pack = ScriptToolPack()
    pack.initialize({"scripts_dir": str(tmp_path / "pack-scripts"), "work_dir": str(tmp_path)})
    return pack


# ------------------------------------------------------------------
# 清单解析
# ------------------------------------------------------------------


def test_manifest_keeps_valid_tools_and_skips_broken_ones(discovery):
    pkg = discovery.get("demo-skill")
    assert [t.name for t in pkg.tools] == ["demo_echo"]
    assert pkg.tools[0].argv == ["--mode", "echo"]
    assert "tools.yaml" not in pkg.resources


def test_python_path_keeps_only_dirs_inside_the_skill(discovery):
    assert discovery.get("demo-skill").python_path == ["scripts"]


def test_skill_without_manifest_has_no_tools(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, manifest="")
    assert SkillDiscovery(roots=[root]).get("demo-skill").tools == []


# ------------------------------------------------------------------
# 执行：参数走 stdin，失败如实标记
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arguments_reach_the_script_via_stdin(discovery, runner):
    out = json.loads(await _tool(discovery, runner).execute({"q": '引号"和中文'}, _scope()))
    assert out == {"ok": True, "argv": ["--mode", "echo"], "got": {"q": '引号"和中文'}}


@pytest.mark.asyncio
async def test_structured_failure_is_returned_without_wrapper(discovery, runner):
    with pytest.raises(ToolCallError) as exc:
        await _tool(discovery, runner).execute({"fail": "json"}, _scope())
    assert json.loads(exc.value.text)["error"]["code"] == "BAD"


@pytest.mark.asyncio
async def test_crash_keeps_the_traceback(discovery, runner):
    with pytest.raises(ToolCallError) as exc:
        await _tool(discovery, runner).execute({"fail": "crash"}, _scope())
    assert exc.value.text.startswith("脚本执行失败")
    assert "boom" in exc.value.text


@pytest.mark.asyncio
async def test_registry_marks_failed_call_as_error(discovery, runner):
    registry = ToolRegistry.from_tools_and_agent([_tool(discovery, runner)], "agent")
    ok = await registry.execute_detailed("demo_echo", {"q": "x"}, _scope(), "agent")
    bad = await registry.execute_detailed("demo_echo", {"fail": "json"}, _scope(), "agent")
    assert ok.status == "success"
    assert bad.status == "error"
    assert json.loads(bad.text)["ok"] is False  # 没被套上「工具 X 执行失败」前缀
    schema = registry.list_tool_schemas_for_agent("agent")[0]["function"]
    assert schema["parameters"]["properties"] == {"q": {"type": "string"}}


# ------------------------------------------------------------------
# 注册
# ------------------------------------------------------------------


def test_build_skips_conflicts_disabled_skills_and_missing_runner(tmp_path, discovery):
    packs = {"script_executor": _script_pack(tmp_path)}
    assert [t.name for t in build_skill_tools(discovery, packs, taken=set())] == ["demo_echo"]
    assert build_skill_tools(discovery, packs, taken={"demo_echo"}) == []
    assert build_skill_tools(discovery, {}, taken=set()) == []
    disabled = SkillDiscovery(roots=discovery.roots, allowlist=frozenset())
    assert build_skill_tools(disabled, packs, taken=set()) == []


def test_fund_product_manifest_registers_five_tools(tmp_path):
    import yaml

    found = SkillDiscovery(roots=[_REPO_SKILLS], allowlist=frozenset({"fund-product-query"}))
    tools = build_skill_tools(found, {"script_executor": _script_pack(tmp_path)}, taken=set())
    assert [t.name for t in tools] == [
        "product_name_lookup", "product_lookup", "product_entity_query", "product_siblings", "product_make_report",
    ]
    # tools.yaml 的实体枚举必须与 entities.yaml 里 product 领域的实体一一对应，免得两边改漏一处
    entities_yaml = _REPO_SKILLS / "fund-product-query" / "scripts" / "entities.yaml"
    entities = yaml.safe_load(entities_yaml.read_text(encoding="utf-8"))["entities"]
    product_entities = sorted(name for name, cfg in entities.items() if cfg.get("domain") == "product")
    assert sorted(tools[2].parameters_schema["properties"]["entity"]["enum"]) == product_entities
    json.dumps([t.parameters_schema for t in tools], ensure_ascii=False)


def test_internal_research_manifest_registers_seven_tools(tmp_path):
    found = SkillDiscovery(roots=[_REPO_SKILLS], allowlist=frozenset({"internal-research"}))
    tools = build_skill_tools(found, {"script_executor": _script_pack(tmp_path)}, taken=set())
    assert [t.name for t in tools] == [
        "research_reports", "research_record", "research_content", "research_download",
        "research_search", "research_context", "research_capabilities",
    ]
    json.dumps([t.parameters_schema for t in tools], ensure_ascii=False)


def test_fund_research_manifest_registers_four_tools(tmp_path):
    found = SkillDiscovery(roots=[_REPO_SKILLS], allowlist=frozenset({"fund-research-db"}))
    tools = build_skill_tools(found, {"script_executor": _script_pack(tmp_path)}, taken=set())
    assert [t.name for t in tools] == ["fund_db_get", "fund_db_list", "fund_db_fields", "fund_db_values"]
    assert len(tools[1].parameters_schema["properties"]["entity"]["enum"]) == 19
    json.dumps([t.parameters_schema for t in tools], ensure_ascii=False)  # 能原样发给模型
    assert found.get("fund-research-db").python_path == ["scripts"]


# ------------------------------------------------------------------
# load_skill 与 python-path
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skill_lists_tools_and_allows_them(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, frontmatter="allowed-tools: [read_file]\n")
    found = SkillDiscovery(roots=[root])
    set_skill_discovery_getter(lambda: found)
    text = await LoadSkillTool(found, enforce_allowed_tools=True).execute({"name": "demo-skill"}, _scope())
    assert "demo_echo" in text
    assert delegation_scope_var.get().allowed_tools == {"read_file", "demo_echo"}


# ------------------------------------------------------------------
# run_script 失败：报错在前、去掉 runner 栈帧、审计记 error
# ------------------------------------------------------------------

_CRASH = """\
print("partial output")
def inner():
    raise KeyError("nav")
inner()
"""


@pytest.fixture()
def crash_runner(tmp_path):
    scripts = tmp_path / "session-scripts"
    (scripts / "test-session").mkdir(parents=True)
    (scripts / "test-session" / "crash.py").write_text(_CRASH, encoding="utf-8")
    return ScriptTool(scripts_dir=scripts, allowed_extensions=[".py"], timeout_seconds=20)


@pytest.mark.asyncio
async def test_run_script_failure_leads_with_the_exception(crash_runner):
    with pytest.raises(ToolCallError) as exc:
        await crash_runner.execute({"script": "crash.py"}, _scope())
    text = exc.value.text
    assert text.splitlines()[0] == "脚本执行失败 (exit=1)：KeyError: 'nav'"
    assert "script_argv_runner" not in text
    assert "runpy" not in text
    assert 'crash.py", line 3, in inner' in text  # 用户自己的栈帧留着
    assert text.index("Traceback") < text.index("--- 出错前脚本的输出 ---") < text.index("partial output")


@pytest.mark.asyncio
async def test_run_script_failure_is_an_error_in_the_registry(crash_runner):
    registry = ToolRegistry.from_tools_and_agent([crash_runner], "agent")
    out = await registry.execute_detailed("run_script", {"script": "crash.py"}, _scope(), "agent")
    assert out.status == "error"
    assert out.text.startswith("脚本执行失败")


def test_clean_traceback_drops_runner_and_runpy_frames():
    from csagent.tools.packs.script_pack import _clean_traceback, _exception_line

    raw = (
        "Traceback (most recent call last):\n"
        '  File "D:\\app\\src\\csagent\\tools\\script_argv_runner.py", line 40, in <module>\n'
        '    runpy.run_path(job["script"], run_name="__main__")\n'
        '  File "<frozen runpy>", line 291, in run_path\n'
        '  File "/usr/lib/python3.12/runpy.py", line 88, in _run_code\n'
        "    exec(code, run_globals)\n"
        '  File "/w/scripts/a.py", line 2, in <module>\n'
        '    x = d["nav"]\n'
        "        ~^^^^^^^\n"
        "KeyError: 'nav'\n"
    )
    cleaned = _clean_traceback(raw)
    assert cleaned.splitlines() == [
        "Traceback (most recent call last):",
        '  File "/w/scripts/a.py", line 2, in <module>',
        '    x = d["nav"]',
        "        ~^^^^^^^",
        "KeyError: 'nav'",
    ]
    assert _exception_line(cleaned) == "KeyError: 'nav'"
    assert _exception_line("警告：只是一行普通的 stderr\n") == ""


@pytest.mark.asyncio
async def test_session_script_can_import_skill_modules(tmp_path, discovery):
    scripts = tmp_path / "session-scripts"
    writer = WriteScriptTool(scripts_dir=scripts, allowed_extensions=[".py"])
    runner = ScriptTool(scripts_dir=scripts, allowed_extensions=[".py"], timeout_seconds=20)
    await writer.execute(
        {"path": "use_helper.py", "content": "import helpermod\nprint(helpermod.GREETING)\n"}, _scope()
    )
    assert "hello-from-skill" in await runner.execute({"script": "use_helper.py"}, _scope())
