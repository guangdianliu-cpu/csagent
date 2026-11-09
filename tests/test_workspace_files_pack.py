"""WorkspaceFilesToolPack 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.channels.workspace_uploads import bind_session_workspace_path
from csagent.react.models import CustomerScope
from csagent.tools.pack_loader import ToolPackLoader
from csagent.tools.packs.workspace_files_pack import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WorkspaceFilesToolPack,
    WriteFileTool,
    resolve_workspace_path,
)


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s-ws", channel="http_api")


def test_resolve_blocks_traversal(tmp_path: Path):
    bad = resolve_workspace_path(tmp_path, "../outside.txt")
    assert isinstance(bad, str)
    assert "越出" in bad


def test_bind_session_workspace_path():
    assert bind_session_workspace_path(".", "s-ws") == "data/uploads/s-ws"
    assert (
        bind_session_workspace_path("测试文件.txt", "s-ws", for_write=True)
        == "data/uploads/s-ws/outbound/测试文件.txt"
    )
    assert (
        bind_session_workspace_path("data/uploads/other/a.txt", "s-ws", for_write=True)
        == "data/uploads/s-ws/a.txt"
    )
    assert (
        bind_session_workspace_path(
            "data/uploads/other/inbound/a.txt", "s-ws", for_write=True
        )
        == "data/uploads/s-ws/inbound/a.txt"
    )
    assert bind_session_workspace_path("scripts/tools/x.py", "s-ws") == "scripts/tools/x.py"
    # skills/<name>/ 改写成 Skill 路径：形态白名单外、未启用的 Skill 在解析时被拒
    assert (
        bind_session_workspace_path(
            "skills/fund-research-db/scripts/backend.py", "s-ws"
        )
        == "<skill:fund-research-db>/scripts/backend.py"
    )
    assert (
        bind_session_workspace_path("scripts/tools/x.py", "s-ws", for_write=True)
        == "data/uploads/s-ws/outbound/scripts/tools/x.py"
    )
    # 裸名读取：默认指向 inbound（文件未必存在）
    assert (
        bind_session_workspace_path("报告.xlsx", "s-ws", for_write=False)
        == "data/uploads/s-ws/inbound/报告.xlsx"
    )
    # send_file 反过来偏 outbound
    assert (
        bind_session_workspace_path(
            "报告.xlsx", "s-ws", for_write=False, prefer_outbound=True
        )
        == "data/uploads/s-ws/outbound/报告.xlsx"
    )


def test_bind_read_no_longer_escapes_to_internal_project_paths():
    """源码、配置、文档、审计日志不再对读类工具放行：一律绑回本会话目录（不存在 → 读不到）。"""
    for raw in (
        "data/audit/sessions-2026-09-28.jsonl",
        "configs/profiles/product/agent.md",
        "src/csagent/main.py",
        "docs/当前实现说明.md",
        "scripts/start.py",
    ):
        bound = bind_session_workspace_path(raw, "s-ws")
        assert bound.startswith("data/uploads/s-ws/"), (raw, bound)


def test_bind_read_rewrites_other_session_scripts_to_own():
    assert (
        bind_session_workspace_path("scripts/tools/web_other/steal.py", "s-ws")
        == "scripts/tools/s-ws/steal.py"
    )
    assert bind_session_workspace_path("scripts/tools", "s-ws") == "scripts/tools/s-ws"
    assert bind_session_workspace_path("skills/demo", "s-ws") == "<skill:demo>"


def test_read_escape_prefixes_can_be_opened_explicitly():
    from csagent.channels.workspace_uploads import configure_read_escape_prefixes

    configure_read_escape_prefixes(["docs", "configs/"])
    try:
        assert bind_session_workspace_path("docs/a.md", "s-ws") == "docs/a.md"
        assert bind_session_workspace_path("configs/base.yaml", "s-ws") == "configs/base.yaml"
        # 前缀按目录边界匹配：docs 不会放行 docs_private/
        assert bind_session_workspace_path("docs_private/x.md", "s-ws").startswith(
            "data/uploads/s-ws/"
        )
    finally:
        configure_read_escape_prefixes(())


@pytest.mark.asyncio
async def test_read_file_cannot_read_audit_log_of_other_users(
    tmp_path: Path, scope: CustomerScope
):
    """以前任何会话都能 read_file data/audit/ 读到全量审计；现在被绑回本会话目录。"""
    audit = tmp_path / "data" / "audit"
    audit.mkdir(parents=True)
    (audit / "sessions-2026-09-28.jsonl").write_text(
        '{"session_id": "web:victim", "user_message": "私密问题"}\n', encoding="utf-8"
    )
    pack = WorkspaceFilesToolPack()
    pack.initialize({"root_dir": str(tmp_path), "session_scoped": True})
    tools = {t.name: t for t in pack.get_tools()}
    out = await tools["read_file"].execute(
        {"path": "data/audit/sessions-2026-09-28.jsonl"}, scope
    )
    assert "私密问题" not in out
    assert out.startswith("错误")
    listing = await tools["list_dir"].execute({"path": "data/audit/"}, scope)
    assert "sessions-2026-09-28.jsonl" not in listing


@pytest.mark.asyncio
async def test_send_file_refuses_file_containing_known_secret(
    tmp_path: Path, scope: CustomerScope, monkeypatch: pytest.MonkeyPatch
):
    from csagent.common.secrets import SecretMasker

    secret = "sk-live-0123456789abcdef"
    monkeypatch.setattr("csagent.common.secrets._masker", SecretMasker([secret]))
    pack = WorkspaceFilesToolPack()
    pack.initialize({"root_dir": str(tmp_path), "session_scoped": True, "send_enabled": True})
    tools = {t.name: t for t in pack.get_tools()}
    await tools["write_file"].execute({"path": "leak.txt", "content": f"key={secret}"}, scope)
    await tools["write_file"].execute({"path": "ok.txt", "content": "普通交付物"}, scope)

    refused = await tools["send_file"].execute({"path": "leak.txt"}, scope)
    assert refused.startswith("错误") and "密钥" in refused
    accepted = await tools["send_file"].execute({"path": "ok.txt"}, scope)
    assert "已加入出站队列" in accepted


def test_workspace_pack_follows_project_config():
    """项目配置里 send_enabled: true（大名单核对要把 xlsx 发回用户）。

    改这条前先想清楚：关掉 send_file，SKILL.md 里「>15 只出表发文件」那条路就断了。
    """
    loader = ToolPackLoader()
    tools = loader.load_tools(["workspace_files"])
    assert [t.name for t in tools] == [
        "list_dir",
        "read_file",
        "write_file",
        "edit_file",
        "send_file",
    ]


def test_workspace_pack_includes_send_file_when_enabled(tmp_path: Path):
    pack = WorkspaceFilesToolPack()
    pack.initialize({"root_dir": str(tmp_path), "send_enabled": True})
    assert [t.name for t in pack.get_tools()] == [
        "list_dir",
        "read_file",
        "write_file",
        "edit_file",
        "send_file",
    ]


@pytest.mark.asyncio
async def test_list_read_write_edit_roundtrip(tmp_path: Path, scope: CustomerScope):
    pack = WorkspaceFilesToolPack()
    pack.initialize({"root_dir": str(tmp_path), "allow_absolute": False})
    by_name = {t.name: t for t in pack.get_tools()}

    written = await by_name["write_file"].execute(
        {"path": "notes/hello.md", "content": "# hi\nline2\n"},
        scope,
    )
    assert "已写入" in written
    # Agent 产出统一落 outbound/
    assert "data/uploads/s-ws/outbound/notes/hello.md" in written.replace("\\", "/")
    assert (
        tmp_path / "data" / "uploads" / "s-ws" / "outbound" / "notes" / "hello.md"
    ).is_file()

    listed = await by_name["list_dir"].execute({"path": "outbound/notes"}, scope)
    assert "hello.md" in listed
    assert "data/uploads/s-ws/outbound/notes" in listed.replace("\\", "/")

    read = await by_name["read_file"].execute({"path": "outbound/notes/hello.md"}, scope)
    assert "# hi" in read
    assert "line2" in read

    edited = await by_name["edit_file"].execute(
        {
            "path": "outbound/notes/hello.md",
            "old_string": "line2",
            "new_string": "line2-edited",
        },
        scope,
    )
    assert "已编辑" in edited
    assert "line2-edited" in (
        tmp_path / "data" / "uploads" / "s-ws" / "outbound" / "notes" / "hello.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_under_uploads_hints_send_file(tmp_path: Path, scope: CustomerScope):
    tool = WriteFileTool(root=tmp_path, send_enabled=True)
    result = await tool.execute(
        {"path": "a.txt", "content": "x\n"},
        scope,
    )
    assert "已写入" in result
    assert "data/uploads/s-ws/outbound/a.txt" in result.replace("\\", "/")
    assert "send_file" in result
    assert "明确要求" in result


@pytest.mark.asyncio
async def test_session_scoped_false_keeps_raw_path(tmp_path: Path, scope: CustomerScope):
    tool = WriteFileTool(root=tmp_path, session_scoped=False)
    result = await tool.execute(
        {"path": "notes/hello.md", "content": "# hi\n"},
        scope,
    )
    assert "已写入" in result
    assert (tmp_path / "notes" / "hello.md").is_file()


@pytest.mark.asyncio
async def test_edit_requires_unique_match(tmp_path: Path, scope: CustomerScope):
    path = tmp_path / "data" / "uploads" / "s-ws" / "outbound" / "dup.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("aa\naa\n", encoding="utf-8")
    tool = EditFileTool(root=tmp_path)
    result = await tool.execute(
        {"path": "dup.txt", "old_string": "aa", "new_string": "bb"},
        scope,
    )
    assert "匹配到 2 处" in result

    ok = await tool.execute(
        {
            "path": "dup.txt",
            "old_string": "aa",
            "new_string": "bb",
            "replace_all": True,
        },
        scope,
    )
    assert "替换 2 处" in ok
    assert path.read_text(encoding="utf-8") == "bb\nbb\n"


@pytest.mark.asyncio
async def test_denies_env_file(tmp_path: Path, scope: CustomerScope):
    env_path = tmp_path / "data" / "uploads" / "s-ws" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("SECRET=1\n", encoding="utf-8")
    read = ReadFileTool(root=tmp_path)
    write = WriteFileTool(root=tmp_path)
    assert "敏感" in await read.execute({"path": ".env"}, scope)
    assert "敏感" in await write.execute({"path": ".env", "content": "x"}, scope)


@pytest.mark.asyncio
async def test_list_skips_denied_names(tmp_path: Path, scope: CustomerScope):
    sess = tmp_path / "data" / "uploads" / "s-ws"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / ".env").write_text("x\n", encoding="utf-8")
    (sess / "ok.txt").write_text("y\n", encoding="utf-8")
    tool = ListDirTool(root=tmp_path)
    listed = await tool.execute({"path": "."}, scope)
    assert "ok.txt" in listed
    assert ".env" not in listed
