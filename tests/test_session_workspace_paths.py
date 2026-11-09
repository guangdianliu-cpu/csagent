"""会话工作区跨树寻址与脚本运行时契约。

会话工作区横跨两棵树：``data/uploads/{session}/``（附件与交付物）和
``scripts/tools/{session}/``（会话脚本）。模型给的相对路径不带树的归属，
过去一律被绑到 uploads 下，导致 write_script 刚写出的脚本，read_file/edit_file
永远报「文件不存在」，模型只能从头重写——本文件锁定修复后的语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.channels.workspace_uploads import (
    bind_session_workspace_path,
    session_probe_candidates,
    session_scripts_rel,
)
from csagent.react.models import CustomerScope
from csagent.tools.packs.script_pack import ScriptTool, WriteScriptTool, _build_scope_env
from csagent.tools.packs.workspace_files_pack import EditFileTool, ReadFileTool


SESSION = "wecom:BoWen"
SEG = "wecom_BoWen"


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id=SESSION, channel="wecom")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """最小工作区：uploads 两个子目录 + 会话脚本目录。"""
    (tmp_path / "data" / "uploads" / SEG / "inbound").mkdir(parents=True)
    (tmp_path / "data" / "uploads" / SEG / "outbound").mkdir(parents=True)
    (tmp_path / "scripts" / "tools" / SEG).mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------- 路径语义


def test_session_scripts_rel_uses_safe_segment():
    assert session_scripts_rel(SESSION) == f"scripts/tools/{SEG}"


def test_probe_candidates_span_uploads_and_scripts_trees():
    probed = session_probe_candidates("inspect_db.py", SESSION)
    assert f"data/uploads/{SEG}/inbound/inspect_db.py" in probed
    assert f"data/uploads/{SEG}/outbound/inspect_db.py" in probed
    assert f"scripts/tools/{SEG}/inspect_db.py" in probed


def test_probe_candidates_respect_session_prefixed_script_path():
    """模型常按 run_script 的写法给 ``{session}/x.py``，不应被再套一层前缀。"""
    probed = session_probe_candidates(f"{SEG}/analyze.py", SESSION)
    assert f"scripts/tools/{SEG}/analyze.py" in probed


def test_probe_candidates_are_deduped_and_ordered():
    probed = session_probe_candidates("a.txt", SESSION, prefer_outbound=True)
    assert len(probed) == len(set(probed))
    assert probed[0] == f"data/uploads/{SEG}/outbound/a.txt"


def test_read_resolves_to_existing_session_script(workspace: Path):
    (workspace / "scripts" / "tools" / SEG / "analyze.py").write_text("x", encoding="utf-8")
    bound = bind_session_workspace_path(
        f"{SEG}/analyze.py", SESSION, for_write=False, project_root=workspace
    )
    assert bound == f"scripts/tools/{SEG}/analyze.py"


def test_edit_resolves_to_existing_session_script(workspace: Path):
    """edit_file 走 for_write=True，但目标必须先被找到，不能盲绑到 outbound。"""
    (workspace / "scripts" / "tools" / SEG / "analyze.py").write_text("x", encoding="utf-8")
    bound = bind_session_workspace_path(
        f"{SEG}/analyze.py",
        SESSION,
        for_write=True,
        probe_existing=True,
        project_root=workspace,
    )
    assert bound == f"scripts/tools/{SEG}/analyze.py"


def test_write_does_not_probe_into_scripts_tree(workspace: Path):
    """write_file 是「新建/覆盖」，落点必须确定，不能被同名脚本吸走。"""
    (workspace / "scripts" / "tools" / SEG / "notes.txt").write_text("x", encoding="utf-8")
    bound = bind_session_workspace_path(
        "notes.txt", SESSION, for_write=True, project_root=workspace
    )
    assert bound == f"data/uploads/{SEG}/outbound/notes.txt"


def test_uploads_probe_still_prefers_inbound_for_reads(workspace: Path):
    (workspace / "data" / "uploads" / SEG / "inbound" / "报告.xlsx").write_text("x", encoding="utf-8")
    bound = bind_session_workspace_path(
        "报告.xlsx", SESSION, for_write=False, project_root=workspace
    )
    assert bound == f"data/uploads/{SEG}/inbound/报告.xlsx"


# ---------------------------------------------------------------- 工具行为


@pytest.mark.asyncio
async def test_read_file_finds_script_written_by_write_script(
    workspace: Path, scope: CustomerScope
):
    """write_script → read_file 闭环：这正是日志里断掉的那一环。"""
    writer = WriteScriptTool(
        scripts_dir=workspace / "scripts" / "tools", allowed_extensions=[".py"]
    )
    written = await writer.execute(
        {"path": "analyze.py", "content": "print('hi')\n"}, scope
    )
    assert "已写入脚本" in written

    reader = ReadFileTool(workspace)
    out = await reader.execute({"path": f"{SEG}/analyze.py"}, scope)
    assert "print('hi')" in out


@pytest.mark.asyncio
async def test_edit_file_can_modify_session_script(workspace: Path, scope: CustomerScope):
    target = workspace / "scripts" / "tools" / SEG / "analyze.py"
    target.write_text("DB = 'old'\n", encoding="utf-8")

    editor = EditFileTool(workspace)
    out = await editor.execute(
        {"path": "analyze.py", "old_string": "'old'", "new_string": "'new'"}, scope
    )
    assert "已编辑" in out
    assert target.read_text(encoding="utf-8") == "DB = 'new'\n"


@pytest.mark.asyncio
async def test_missing_file_error_lists_probed_locations(
    workspace: Path, scope: CustomerScope
):
    """报错要说明找过哪里，否则模型无从修正，只会重写文件。"""
    reader = ReadFileTool(workspace)
    out = await reader.execute({"path": "nope.json"}, scope)
    assert "文件不存在" in out
    assert f"scripts/tools/{SEG}/nope.json" in out
    assert f"data/uploads/{SEG}/outbound/nope.json" in out


# ---------------------------------------------------------------- 脚本运行时契约


def test_scope_env_exposes_workspace_contract(scope: CustomerScope, tmp_path: Path):
    env = _build_scope_env(scope, project_root=tmp_path)
    root = tmp_path.resolve()
    assert env["CSAGENT_PROJECT_ROOT"] == str(root)
    assert Path(env["CSAGENT_SESSION_DIR"]) == root / "data" / "uploads" / SEG
    assert Path(env["CSAGENT_INBOUND_DIR"]) == root / "data" / "uploads" / SEG / "inbound"
    assert Path(env["CSAGENT_OUTBOUND_DIR"]) == root / "data" / "uploads" / SEG / "outbound"
    assert Path(env["CSAGENT_SCRIPT_DIR"]) == root / "scripts" / "tools" / SEG
    assert "CSAGENT_TRACE_ID" in env
    assert "CSAGENT_USER_ID" in env
    assert "CSAGENT_AUTH_METHOD" in env
    assert "CSAGENT_PROFILE" in env
    assert "CSAGENT_INSTANCE_ID" in env


def test_scope_env_contract_survives_passthrough_whitelist(
    scope: CustomerScope, tmp_path: Path
):
    """白名单模式过滤的是「继承来的」环境变量，约定变量必须照常注入。"""
    env = _build_scope_env(scope, env_passthrough=[], project_root=tmp_path)
    assert env["CSAGENT_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert env["CSAGENT_SCRIPT_DIR"]


@pytest.mark.asyncio
async def test_session_script_runs_with_project_root_as_cwd(
    workspace: Path, scope: CustomerScope
):
    """run_script 与 run_shell 的 cwd 必须一致，否则同一条相对路径两个结果。"""
    (workspace / "data" / "marker.txt").write_text("ok", encoding="utf-8")
    script = workspace / "scripts" / "tools" / SEG / "probe.py"
    script.write_text(
        "import os\nprint(os.path.exists('data/marker.txt'))\n"
        "print(os.environ['CSAGENT_PROJECT_ROOT'])\n",
        encoding="utf-8",
    )

    tool = ScriptTool(
        scripts_dir=workspace / "scripts" / "tools",
        allowed_extensions=[".py"],
        work_dir=workspace,
    )
    out = await tool.execute({"script": "probe.py"}, scope)
    assert "True" in out
    assert str(workspace.resolve()) in out


def test_bind_absolute_path_under_root_normalizes_cleanly(workspace: Path):
    """传入位于 project_root 下的绝对路径应正确规范化为相对路径，避免嵌套畸变。"""
    target = workspace / "data" / "uploads" / SEG / "outbound" / "preview" / "slide1.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PNG_DATA")

    bound = bind_session_workspace_path(
        str(target.resolve()),
        SESSION,
        for_write=False,
        project_root=workspace,
    )
    assert bound == f"data/uploads/{SEG}/outbound/preview/slide1.png"

    # 外部路径保持原样，交给上层安全拦截
    external = Path("C:/Windows/System32/cmd.exe").resolve()
    bound_ext = bind_session_workspace_path(
        str(external),
        SESSION,
        for_write=False,
        project_root=workspace,
    )
    assert bound_ext == str(external)

