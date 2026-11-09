"""读类工具的可见性边界：read_file / list_dir 与 grep / glob 对「能看见什么」口径一致。

钉死的边界（相对工作区根）：
- 本会话目录可见；其他会话的上传与脚本不可见；
- 审计日志、记忆库、LLM 原文等运行时数据不可见；
- ``.env*`` 等点文件与敏感文件名不可见（以前 grep 的 Python 回退分支能把密钥逐行打出来）；
- 形态白名单外 / 未启用的 Skill 不可见；
- 源码、配置、文档默认不可见，可经 ``read_escape_prefixes`` 显式放行。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from csagent.channels.workspace_uploads import configure_read_escape_prefixes
from csagent.react.models import CustomerScope
from csagent.tools.packs.search_pack import GlobTool, GrepTool
from csagent.tools.visibility import is_downloadable_by_grant, is_visible_to_session

SESSION = "web:me"
SEG = "web_me"
MARK = "VISIBILITY_MARK"


class _Pkg:
    def __init__(self, root: Path, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled


class _Discovery:
    def __init__(self, packages: dict[str, _Pkg]) -> None:
        self._packages = packages
        self.roots: list[Path] = []

    def get(self, name: str) -> _Pkg | None:
        return self._packages.get(name)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    files = {
        f"data/uploads/{SEG}/inbound/mine.txt": "own upload",
        "data/uploads/web_other/inbound/theirs.txt": "other user's upload",
        "data/audit/sessions-2026-09-28.jsonl": "everyone's questions",
        "data/llm_bodies/x.json": "raw llm bodies",
        ".env.research": "WECOM_SECRET=leak",
        "keys/server.pem": "private key",
        "skills/visible-skill/SKILL.md": "visible skill",
        "skills/hidden-skill/data/db.txt": "hidden skill data",
        f"scripts/tools/{SEG}/mine.py": "own script",
        "scripts/tools/web_other/theirs.py": "other script",
        "scripts/tools/shared.py": "shared script",
        "scripts/start.py": "ops script",
        "configs/base.yaml": "config",
        "src/app.py": "source",
        "README.md": "readme",
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{MARK} {body}\n", encoding="utf-8")

    discovery = _Discovery({
        "visible-skill": _Pkg(tmp_path / "skills" / "visible-skill", enabled=True),
        "hidden-skill": _Pkg(tmp_path / "skills" / "hidden-skill", enabled=False),
    })
    monkeypatch.setattr(
        "csagent.tools.skill_paths._skill_discovery_getter_fn", lambda: discovery
    )
    return tmp_path


VISIBLE = {
    f"data/uploads/{SEG}/inbound/mine.txt",
    "skills/visible-skill/SKILL.md",
    f"scripts/tools/{SEG}/mine.py",
    "scripts/tools/shared.py",
    "README.md",
}
HIDDEN = {
    "data/uploads/web_other/inbound/theirs.txt",
    "data/audit/sessions-2026-09-28.jsonl",
    "data/llm_bodies/x.json",
    ".env.research",
    "keys/server.pem",
    "skills/hidden-skill/data/db.txt",
    "scripts/tools/web_other/theirs.py",
    "scripts/start.py",
    "configs/base.yaml",
    "src/app.py",
}


def _scope() -> CustomerScope:
    return CustomerScope(session_id=SESSION, channel="test")


def test_visibility_predicate(project: Path) -> None:
    for rel in VISIBLE:
        assert is_visible_to_session(rel, SESSION), rel
    for rel in HIDDEN:
        assert not is_visible_to_session(rel, SESSION), rel
    # 目录：通往本会话目录的路径可以穿过，别人的不行
    assert is_visible_to_session("data", SESSION, is_dir=True)
    assert is_visible_to_session("data/uploads", SESSION, is_dir=True)
    assert not is_visible_to_session("data/audit", SESSION, is_dir=True)
    assert not is_visible_to_session("data/uploads/web_other", SESSION, is_dir=True)
    assert not is_visible_to_session("scripts/tools/web_other", SESSION, is_dir=True)


def test_escape_prefix_opens_internal_dir_explicitly(project: Path) -> None:
    assert not is_visible_to_session("configs/base.yaml", SESSION)
    configure_read_escape_prefixes(["configs/"])
    assert is_visible_to_session("configs/base.yaml", SESSION)
    # 放行前缀也盖不过敏感文件名与点文件
    assert not is_visible_to_session(".env.research", SESSION)


def _shown_files(output: str) -> set[str]:
    return {line.split(":", 1)[0].replace("\\", "/") for line in output.splitlines() if ":" in line}


@pytest.mark.asyncio
async def test_grep_python_fallback_only_returns_visible_files(project: Path) -> None:
    tool = GrepTool(project)
    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        out = await tool.execute({"pattern": MARK}, _scope())
    shown = _shown_files(out)
    assert shown == VISIBLE, out
    assert "leak" not in out


@pytest.mark.asyncio
async def test_grep_rejects_search_root_outside_scope(project: Path) -> None:
    tool = GrepTool(project)
    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        for path in ("data/audit", "data/uploads/web_other", "skills/hidden-skill", "configs"):
            out = await tool.execute({"pattern": MARK, "path": path}, _scope())
            assert out.startswith("错误") and "范围" in out, (path, out)


def test_rg_output_is_filtered_by_visibility(project: Path) -> None:
    """rg 只认 .gitignore 与隐藏文件规则；部署目录不是 git 仓库时会扫进 data/，靠逐行过滤兜住。"""
    tool = GrepTool(project)
    root = project.resolve()
    raw = []
    for rel in sorted(VISIBLE | HIDDEN):
        raw.append(f"{root / rel}\x001:{MARK} line")
    raw.append("--")
    raw.append(f"{root / 'README.md'}\x002-context line")
    kept = tool._visible_rg_lines(raw, root, SESSION)
    # 匹配行「路径:行号:内容」，上下文行「路径-行号-内容」
    shown = {
        m.group(1).replace("\\", "/")
        for line in kept
        if (m := re.match(r"^(.+?)[:-]\d+[:-]", line))
    }
    assert shown == VISIBLE
    assert any(line.startswith("README.md-2-context") for line in kept)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="本机没有 ripgrep")
async def test_grep_ripgrep_only_returns_visible_files(project: Path) -> None:
    tool = GrepTool(project)
    out = await tool.execute({"pattern": MARK}, _scope())
    shown = _shown_files(out)
    assert shown <= VISIBLE, out
    assert "leak" not in out


@pytest.mark.asyncio
async def test_glob_only_returns_visible_files(project: Path) -> None:
    tool = GlobTool(project)
    out = await tool.execute({"pattern": "**/*"}, _scope())
    shown = {line.replace("\\", "/") for line in out.splitlines() if line.strip()}
    assert shown == VISIBLE, out


@pytest.mark.asyncio
async def test_glob_rejects_parent_traversal(project: Path) -> None:
    out = await GlobTool(project).execute({"pattern": "../**/*"}, _scope())
    assert out.startswith("错误")


def test_grant_download_scope(project: Path) -> None:
    assert is_downloadable_by_grant(f"data/uploads/{SEG}/outbound/report.xlsx")
    assert is_downloadable_by_grant("data/uploads/web_other/outbound/report.xlsx")
    assert is_downloadable_by_grant("scripts/tools/shared.py")
    assert is_downloadable_by_grant("skills/visible-skill/SKILL.md")
    for rel in (
        "data/audit/sessions-2026-09-28.jsonl",
        "data/csagent.db",
        "data/uploads/report.xlsx",
        "skills/hidden-skill/data/db.txt",
        "configs/base.yaml",
        ".env.research",
    ):
        assert not is_downloadable_by_grant(rel), rel
