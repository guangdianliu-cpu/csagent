"""ScriptToolPack / WebSearchToolPack / FileHandler 单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from csagent.channels.file_handler import FileHandler
from csagent.channels.models import Attachment, AttachmentType
from csagent.react.models import CustomerScope
from csagent.tools.base import ToolCallError
from csagent.tools.pack_loader import ToolPackLoader
from csagent.tools.packs.script_pack import (
    ScriptTool,
    ScriptToolPack,
    ShellTool,
    WriteScriptTool,
    _ARGV_RUNNER,
)
from csagent.tools.script_argv_runner import main as argv_runner_main
from csagent.tools.packs.web_search_pack import WebSearchTool, WebSearchToolPack


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s-test", channel="http_api")


@pytest.mark.asyncio
async def test_script_pack_loads_configured_tools(scope: CustomerScope, monkeypatch):
    """run_shell 已在 configs/tool_packs.yaml 关闭（shell_enabled: false）。

    它无沙箱、cwd 是项目根，而全仓没有任何 Skill 用它——每一处引用都是禁令。
    这条断言同时起到看门作用：谁把它重新打开，这里会先红。

    这里只看 tool_packs.yaml 本身，不叠加形态覆盖：product 形态关了 write_script，
    结果会随默认形态变（形态覆盖由下一条单独断言）。
    """
    monkeypatch.setattr(
        "csagent.infra.agents_config.load_tool_pack_overrides", lambda: {}
    )
    loader = ToolPackLoader()
    tools = loader.load_tools(["script_executor"])
    names = [t.name for t in tools]
    assert names == ["write_script", "run_script"]
    assert "run_shell" not in names



@pytest.mark.asyncio
async def test_product_profile_only_runs_skill_scripts(scope: CustomerScope, monkeypatch):
    """product 形态覆盖 script_executor：不给 write_script，run_shell 照样关着。"""
    monkeypatch.setenv("AGENT_PROFILE", "product")
    names = [t.name for t in ToolPackLoader().load_tools(["script_executor"])]
    assert names == ["run_script"]

@pytest.mark.asyncio
async def test_write_then_run_script(tmp_path: Path, scope: CustomerScope):
    write_tool = WriteScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"])
    run_tool = ScriptTool(
        scripts_dir=tmp_path,
        allowed_extensions=[".py"],
        timeout_seconds=10,
    )
    written = await write_tool.execute(
        {
            "path": "echo_demo.py",
            "content": (
                "import os, sys\n"
                "print('echo_demo ok')\n"
                "print('SESSION_DIR=' + os.environ.get('CSAGENT_SESSION_DIR', ''))\n"
                "print(' '.join(sys.argv[1:]))\n"
            ),
        },
        scope,
    )
    assert "已写入" in written
    assert "s-test/echo_demo.py" in written.replace("\\", "/")
    assert (tmp_path / "s-test" / "echo_demo.py").is_file()

    listed = await run_tool.execute({"action": "list"}, scope)
    assert "s-test/echo_demo.py" in listed.replace("\\", "/")

    output = await run_tool.execute(
        {"script": "echo_demo.py", "args": ["hello", "world"]},
        scope,
    )
    assert "echo_demo ok" in output
    # CUSTOMER_ID 已随 customer_id 整条链路移除；工作区契约变量仍应注入
    assert "SESSION_DIR=" in output
    assert "s-test" in output  # 注入的是本会话目录，不是空串
    assert "hello" in output


def test_script_argv_runner_restores_quoted_json_payload(tmp_path: Path, capsys):
    """job 文件里的 JSON 必须原样回到 sys.argv，不能再走命令行拆引号。"""
    script = tmp_path / "echo_payload.py"
    script.write_text(
        "import json, sys\n"
        "print(sys.argv[1])\n"
        "print(sys.argv[2])\n"
        "json.loads(sys.argv[2])\n",
        encoding="utf-8",
    )
    payload = '{"fund_codes":["620010"],"custtype":0}'
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"script": str(script), "argv": ["--payload", payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    saved_argv = sys.argv
    try:
        argv_runner_main([str(job)])
    finally:
        sys.argv = saved_argv
    out = capsys.readouterr().out
    assert "--payload" in out
    assert payload in out


@pytest.mark.asyncio
async def test_run_script_preserves_json_payload_quotes(tmp_path: Path, scope: CustomerScope):
    """周会踩坑：--payload JSON 被 pydevd 再解析命令行后丢掉引号。"""
    (tmp_path / "echo_payload.py").write_text(
        "import json, sys\n"
        "print(sys.argv[1])\n"
        "print(sys.argv[2])\n"
        "obj = json.loads(sys.argv[2])\n"
        "print('codes=' + ','.join(obj['fund_codes']))\n",
        encoding="utf-8",
    )
    payload = '{"fund_codes":["620010","620007"],"custtype":0}'
    tool = ScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"], timeout_seconds=10)
    out = await tool.execute(
        {"script": "echo_payload.py", "args": ["--payload", payload]},
        scope,
    )
    assert payload in out
    assert "codes=620010,620007" in out


def test_python_command_line_does_not_embed_payload(tmp_path: Path):
    script = tmp_path / "x.py"
    script.write_text("print(1)\n", encoding="utf-8")
    tool = ScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"])
    payload = '{"fund_codes":["620010"]}'
    cmd, job = tool._build_command(script, ["--action", "multi_lookup", "--payload", payload])
    try:
        assert isinstance(cmd, list)
        assert cmd[0] == sys.executable
        assert Path(cmd[1]).resolve() == _ARGV_RUNNER.resolve()
        joined = " ".join(cmd)
        assert "--payload" not in joined
        assert "fund_codes" not in joined
        assert json.loads(Path(cmd[2]).read_text(encoding="utf-8"))["argv"][3] == payload
    finally:
        if job is not None:
            job.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_write_script_blocks_path_traversal(tmp_path: Path, scope: CustomerScope):
    tool = WriteScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"])
    bad = await tool.execute(
        {"path": "../escape.py", "content": "print(1)\n"},
        scope,
    )
    assert "越出" in bad or "绝对路径" in bad or ".." in bad


@pytest.mark.asyncio
async def test_script_tool_blocks_path_traversal(tmp_path: Path, scope: CustomerScope):
    tool = ScriptTool(
        scripts_dir=tmp_path,
        allowed_extensions=[".py"],
        timeout_seconds=5,
    )
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    with pytest.raises(ToolCallError) as exc:
        await tool.execute({"script": "../ok.py"}, scope)
    bad = exc.value.text
    assert "越出" in bad or "绝对路径" in bad or ".." in bad


@pytest.mark.asyncio
async def test_script_tool_allows_subdir(tmp_path: Path, scope: CustomerScope):
    nested = tmp_path / "s-test" / "utils"
    nested.mkdir(parents=True)
    (nested / "hello.py").write_text("print('nested-ok')\n", encoding="utf-8")
    tool = ScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"], timeout_seconds=5)
    listed = await tool.execute({"action": "list"}, scope)
    assert "s-test/utils/hello.py" in listed.replace("\\", "/")
    out = await tool.execute({"script": "utils/hello.py"}, scope)
    assert "nested-ok" in out


@pytest.mark.asyncio
async def test_script_tool_timeout(tmp_path: Path, scope: CustomerScope):
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(5)\nprint('done')\n", encoding="utf-8")
    tool = ScriptTool(
        scripts_dir=tmp_path,
        allowed_extensions=[".py"],
        timeout_seconds=1,
    )
    with pytest.raises(ToolCallError) as exc:
        await tool.execute({"script": "slow.py"}, scope)
    assert "超时" in exc.value.text


@pytest.mark.asyncio
async def test_run_script_outputs_utf8_chinese_without_mojibake(tmp_path: Path, scope: CustomerScope):
    # Windows 子进程 stdout 默认走活动代码页（常见 GBK），不强制 UTF-8 会和
    # _build_scope_env 里 utf-8 decode 对不上，中文输出变成乱码。
    (tmp_path / "cn.py").write_text(
        "print('生成报表: a.csv -> b.xlsx (标题: 测试报表)')\n", encoding="utf-8"
    )
    tool = ScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"], timeout_seconds=10)
    out = await tool.execute({"script": "cn.py"}, scope)
    assert "生成报表" in out
    assert "�" not in out


@pytest.mark.asyncio
async def test_run_shared_root_script(tmp_path: Path, scope: CustomerScope):
    (tmp_path / "shared.py").write_text("print('shared-ok')\n", encoding="utf-8")
    tool = ScriptTool(scripts_dir=tmp_path, allowed_extensions=[".py"], timeout_seconds=5)
    out = await tool.execute({"script": "shared.py"}, scope)
    assert "shared-ok" in out


@pytest.mark.asyncio
async def test_run_shell_python_one_liner(tmp_path: Path, scope: CustomerScope):
    tool = ShellTool(
        work_dir=tmp_path,
        timeout_seconds=15,
        default_shell="powershell" if sys.platform.startswith("win") else "bash",
    )
    # PowerShell：单引号包住 -c 代码，避免 cmd 吃引号
    if sys.platform.startswith("win"):
        cmd = f"& '{sys.executable}' -c 'print(42)'"
        out = await tool.execute({"command": cmd, "shell": "powershell"}, scope)
    else:
        cmd = f"{sys.executable} -c 'print(42)'"
        out = await tool.execute({"command": cmd, "shell": "bash"}, scope)
    assert "42" in out


@pytest.mark.asyncio
async def test_run_shell_echo(tmp_path: Path, scope: CustomerScope):
    tool = ShellTool(work_dir=tmp_path, timeout_seconds=10, default_shell="cmd")
    if sys.platform.startswith("win"):
        out = await tool.execute({"command": "echo hello-shell", "shell": "cmd"}, scope)
    else:
        out = await tool.execute({"command": "echo hello-shell", "shell": "bash"}, scope)
    assert "hello-shell" in out


@pytest.mark.asyncio
async def test_run_shell_requires_command(tmp_path: Path, scope: CustomerScope):
    tool = ShellTool(work_dir=tmp_path, timeout_seconds=5)
    assert "缺少参数" in await tool.execute({}, scope)


@pytest.mark.asyncio
async def test_web_search_requires_query(scope: CustomerScope):
    tool = WebSearchTool(search_engine="duckduckgo")
    assert "缺少参数" in await tool.execute({}, scope)


@pytest.mark.asyncio
async def test_web_search_duckduckgo_formats_response(scope: CustomerScope):
    tool = WebSearchTool(search_engine="duckduckgo", max_results=3)
    fake_json = {
        "Heading": "Fund",
        "AbstractText": "A mutual fund pools money.",
        "AbstractURL": "https://example.com/fund",
        "RelatedTopics": [
            {"Text": "ETF is related"},
            {"Text": "Index fund"},
        ],
    }

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=fake_json)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("csagent.tools.packs.web_search_pack.httpx.AsyncClient", return_value=mock_client):
        text = await tool.execute({"query": "mutual fund"}, scope)

    assert "duckduckgo" in text
    assert "A mutual fund pools money." in text
    assert "ETF is related" in text


@pytest.mark.asyncio
async def test_web_search_google_without_key_degrades(scope: CustomerScope):
    tool = WebSearchTool(search_engine="google", api_key="")
    text = await tool.execute({"query": "test"}, scope)
    assert "未配置 API Key" in text


def test_web_search_pack_loads_from_yaml():
    pack = WebSearchToolPack()
    pack.initialize({"search_engine": "duckduckgo", "max_results": 3, "tools": []})
    tools = pack.get_tools()
    assert len(tools) == 1
    assert tools[0].name == "web_search"


def test_script_pack_initialize_creates_dir(tmp_path: Path):
    pack = ScriptToolPack()
    target = tmp_path / "scripts" / "tools"
    work = tmp_path / "work"
    pack.initialize(
        {
            "scripts_dir": str(target),
            "work_dir": str(work),
            "allowed_extensions": [".py"],
            "shell_enabled": True,
            "default_shell": "cmd",
        }
    )
    assert target.is_dir()
    assert work.is_dir()
    names = [t.name for t in pack.get_tools()]
    assert names == ["write_script", "run_script", "run_shell"]


def test_script_pack_shell_can_disable(tmp_path: Path):
    pack = ScriptToolPack()
    pack.initialize(
        {
            "scripts_dir": str(tmp_path / "scripts"),
            "shell_enabled": False,
            "allowed_extensions": [".py"],
        }
    )
    names = [t.name for t in pack.get_tools()]
    assert names == ["write_script", "run_script"]


def test_script_pack_write_can_disable(tmp_path: Path):
    pack = ScriptToolPack()
    pack.initialize(
        {
            "scripts_dir": str(tmp_path / "scripts"),
            "write_enabled": False,
            "allowed_extensions": [".py"],
        }
    )
    names = [t.name for t in pack.get_tools()]
    assert names == ["run_script", "run_shell"]


def test_script_pack_write_and_shell_both_disabled(tmp_path: Path):
    pack = ScriptToolPack()
    pack.initialize(
        {
            "scripts_dir": str(tmp_path / "scripts"),
            "write_enabled": False,
            "shell_enabled": False,
            "allowed_extensions": [".py"],
        }
    )
    names = [t.name for t in pack.get_tools()]
    assert names == ["run_script"]


def test_script_pack_default_env_passthrough_is_none(tmp_path: Path):
    """不配置 env_passthrough 时保持现状：ScriptTool/ShellTool 全量继承 os.environ。"""
    pack = ScriptToolPack()
    pack.initialize(
        {"scripts_dir": str(tmp_path / "scripts"), "allowed_extensions": [".py"]}
    )
    tools = {t.name: t for t in pack.get_tools()}
    assert tools["run_script"]._env_passthrough is None
    assert tools["run_shell"]._env_passthrough is None


@pytest.mark.asyncio
async def test_script_pack_env_passthrough_blocks_unrelated_secret(
    tmp_path: Path, scope: CustomerScope, monkeypatch
):
    monkeypatch.setenv("FAKE_SECRET", "leak-me-not")
    pack = ScriptToolPack()
    pack.initialize(
        {
            "scripts_dir": str(tmp_path / "scripts"),
            "allowed_extensions": [".py"],
            "shell_enabled": False,
            "env_passthrough": ["SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC"],
        }
    )
    run_tool = pack.get_tools()[-1]
    assert run_tool.name == "run_script"
    written_path = Path(pack.scripts_dir) / "dump_env.py"
    written_path.write_text(
        "import os\nprint('SECRET=' + os.environ.get('FAKE_SECRET', '<absent>'))\n"
        "print('SESSION=' + os.environ.get('CSAGENT_SESSION_ID', '<absent>'))\n",
        encoding="utf-8",
    )
    out = await run_tool.execute({"script": "dump_env.py"}, scope)
    assert "SECRET=<absent>" in out
    assert f"SESSION={scope.session_id}" in out


@pytest.mark.asyncio
async def test_file_handler_reads_local_text(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("# hello\nworld\n", encoding="utf-8")
    handler = FileHandler()
    result = await handler.process(
        Attachment(
            type=AttachmentType.FILE,
            filename="note.md",
            local_path=str(path),
        )
    )
    assert result.text is not None
    assert "hello" in result.text
    assert "world" in result.text
