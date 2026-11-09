"""test_search_pack.py — grep / glob 工具测试。"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from csagent.react.models import CustomerScope
from csagent.tools.packs.search_pack import GrepTool, GlobTool


def _scope() -> CustomerScope:
    return CustomerScope(session_id="test-session", channel="test")


def _create_files(root: Path) -> None:
    """创建测试文件结构。"""
    (root / "hello.py").write_text("def hello():\n    print('Hello World')\n", encoding="utf-8")
    (root / "world.txt").write_text("Hello World\nGoodbye World\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "deep.py").write_text("# deep module\nimport os\n", encoding="utf-8")
    # 应被排除的目录
    pycache = root / "__pycache__"
    pycache.mkdir()
    (pycache / "cached.pyc").write_bytes(b"\x00\x00compiled")


# ---------- GrepTool (Python 回退) ----------

@pytest.mark.asyncio
async def test_grep_python_fallback_finds_pattern(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)

    # 模拟无 rg
    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute({"pattern": "Hello"}, _scope())

    assert "hello.py" in result
    assert "world.txt" in result


@pytest.mark.asyncio
async def test_grep_python_case_insensitive(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)

    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute(
            {"pattern": "hello", "case_insensitive": True}, _scope()
        )

    assert "Hello" in result or "hello" in result


@pytest.mark.asyncio
async def test_grep_python_with_glob_filter(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)

    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute(
            {"pattern": "Hello", "glob": "*.py"}, _scope()
        )

    assert "hello.py" in result
    assert "world.txt" not in result


@pytest.mark.asyncio
async def test_grep_python_no_match(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)

    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute({"pattern": "NONEXISTENT_PATTERN"}, _scope())

    assert "未找到" in result


@pytest.mark.asyncio
async def test_grep_python_excludes_pycache(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)

    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute({"pattern": "compiled"}, _scope())

    # __pycache__ 应被排除
    assert "未找到" in result


# ---------- 越界路径 ----------

@pytest.mark.asyncio
async def test_grep_rejects_dotdot_path(tmp_path):
    tool = GrepTool(tmp_path)
    result = await tool.execute({"pattern": "test", "path": "../.."}, _scope())
    assert "错误" in result


@pytest.mark.asyncio
async def test_grep_rejects_absolute_path(tmp_path):
    tool = GrepTool(tmp_path)
    result = await tool.execute({"pattern": "test", "path": "/etc/passwd"}, _scope())
    assert "错误" in result


# ---------- GlobTool ----------

@pytest.mark.asyncio
async def test_glob_finds_py_files(tmp_path):
    _create_files(tmp_path)
    tool = GlobTool(tmp_path)
    result = await tool.execute({"pattern": "**/*.py"}, _scope())

    assert "hello.py" in result
    assert "deep.py" in result


@pytest.mark.asyncio
async def test_glob_no_match(tmp_path):
    _create_files(tmp_path)
    tool = GlobTool(tmp_path)
    result = await tool.execute({"pattern": "*.rs"}, _scope())
    assert "未找到" in result


@pytest.mark.asyncio
async def test_glob_rejects_dotdot(tmp_path):
    tool = GlobTool(tmp_path)
    result = await tool.execute({"pattern": "*.py", "path": "../.."}, _scope())
    assert "错误" in result


# ---------- 无效正则 ----------

@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path):
    _create_files(tmp_path)
    tool = GrepTool(tmp_path)
    with patch("csagent.tools.packs.search_pack.shutil.which", return_value=None):
        result = await tool.execute({"pattern": "[invalid"}, _scope())
    assert "错误" in result or "无效" in result
