"""搜索工具包：grep + glob。

grep 优先调用系统 ripgrep (rg)，不可用时回退 Python re 逐文件扫描。
glob 按修改时间倒序返回匹配的文件路径。

两者都只返回对当前会话可见的路径（见 tools/visibility.py）：其他会话的上传与脚本、
审计 / 记忆库 / LLM 原文、``.env*`` 等敏感文件、形态白名单外的 Skill 一律不出现在结果里。
以前 Python 回退分支连点文件都不跳过，``grep SECRET`` 能把 ``.env.*`` 的值逐行打出来。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import structlog

from csagent.common.proc import kill_tree, spawn_kwargs

from csagent.infra.config import PROJECT_ROOT
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.visibility import is_visible_to_session

logger = structlog.get_logger(__name__)

# 默认排除的目录/文件
_DEFAULT_EXCLUDES = {
    ".git", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".eggs", "*.pyc", "*.pyo",
}

_MAX_OUTPUT_LINES = 200


def _is_binary(path: Path) -> bool:
    """简单判断是否为二进制文件（检查前 8KB）。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


def _should_exclude(path: Path, excludes: set[str]) -> bool:
    """检查路径是否应被排除。"""
    parts = path.parts
    for exc in excludes:
        if "*" in exc:
            if any(fnmatch.fnmatch(p, exc) for p in parts):
                return True
        else:
            if exc in parts:
                return True
    return False


def _resolve_search_root(root: Path, user_path: str) -> Path | str:
    """解析搜索路径，防止越界。"""
    raw = str(user_path or ".").strip()
    if raw in {".", "./"}:
        return root.resolve()

    p = Path(raw)
    if p.is_absolute():
        return "错误: 不允许使用绝对路径"

    if any(part == ".." for part in p.parts):
        return "错误: 路径不得包含 .."

    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return "错误: 路径越出工作区"
    return candidate


def _visible(
    root: Path, path: Path, session_id: str, *, is_dir: bool, resolve: bool = False
) -> bool:
    """``path`` 对会话是否可见。``root`` 须已 resolve；越出工作区一律不可见。

    os.walk 产出的路径本就在已解析的搜索根之下，不必逐个 resolve；rg 输出与 glob
    结果可能带符号链接或相对写法，传 ``resolve=True``。
    """
    try:
        target = path.resolve() if resolve else path
        rel = target.relative_to(root).as_posix()
    except (ValueError, OSError):
        return False
    return is_visible_to_session(rel, session_id, is_dir=is_dir)


_OUT_OF_SCOPE = "错误: 该路径不在当前会话可访问的范围内"


class GrepTool(BaseTool):
    """在文件中搜索文本模式。"""

    name = "grep"
    description = (
        "在文件中搜索文本模式。优先使用 ripgrep(rg)，不可用时回退 Python re。"
        "参数: pattern(搜索模式), path(搜索路径,默认'.'), "
        "glob(文件过滤,如'*.py'), case_insensitive(忽略大小写), "
        "max_results(最大结果数), context_lines(上下文行数)"
    )

    def __init__(self, root: Path, *, max_results: int = 100, max_output_lines: int = 200) -> None:
        self._root = root
        self._max_results = max_results
        self._max_output_lines = max_output_lines

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索模式（正则表达式）"},
                "path": {"type": "string", "description": "搜索路径（相对工作区，默认 '.'）"},
                "glob": {"type": "string", "description": "文件过滤（如 '*.py'）"},
                "case_insensitive": {"type": "boolean", "description": "是否忽略大小写"},
                "max_results": {"type": "integer", "description": "最大结果数"},
                "context_lines": {"type": "integer", "description": "显示匹配行前后的上下文行数"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        pattern = str(arguments.get("pattern") or "").strip()
        if not pattern:
            return "错误: 缺少 pattern 参数"

        user_path = str(arguments.get("path") or ".").strip()
        search_root = _resolve_search_root(self._root, user_path)
        if isinstance(search_root, str):
            return search_root
        if not _visible(
            self._root.resolve(), search_root, scope.session_id, is_dir=search_root.is_dir()
        ):
            return _OUT_OF_SCOPE

        glob_filter = str(arguments.get("glob") or "").strip() or None
        case_insensitive = bool(arguments.get("case_insensitive", False))
        max_results = min(int(arguments.get("max_results") or self._max_results), 500)
        context_lines = min(int(arguments.get("context_lines") or 0), 5)

        # 尝试 ripgrep
        rg_path = shutil.which("rg")
        if rg_path:
            return await self._grep_rg(
                rg_path, pattern, search_root, glob_filter,
                case_insensitive, max_results, context_lines,
                session_id=scope.session_id,
            )

        # 回退 Python re
        return self._grep_python(
            pattern, search_root, glob_filter,
            case_insensitive, max_results,
            session_id=scope.session_id,
        )

    async def _grep_rg(
        self, rg_path: str, pattern: str, search_root: Path,
        glob_filter: str | None, case_insensitive: bool,
        max_results: int, context_lines: int,
        *, session_id: str,
    ) -> str:
        # --null：路径后跟 NUL 而不是 ':' / '-'，路径里有冒号（Windows 盘符）也能可靠切开，
        # 才好逐行按可见性过滤
        cmd = [rg_path, "--line-number", "--no-heading", "--color=never", "--null"]
        if case_insensitive:
            cmd.append("-i")
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        cmd.extend(["-m", str(max_results)])
        # 排除常见目录
        for exc in _DEFAULT_EXCLUDES:
            if "*" not in exc:
                cmd.extend(["--glob", f"!{exc}/"])
        cmd.extend([pattern, str(search_root)])

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **spawn_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            await kill_tree(proc)
            return "错误: ripgrep 搜索超时"
        except asyncio.CancelledError:
            await kill_tree(proc)
            raise
        except Exception as exc:
            logger.debug("rg_failed_fallback_python", error=str(exc))
            return self._grep_python(
                pattern, search_root, glob_filter, case_insensitive, max_results,
                session_id=session_id,
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return "未找到匹配结果。"

        lines = self._visible_rg_lines(output.splitlines(), search_root, session_id)
        if not lines:
            return "未找到匹配结果。"

        result = "\n".join(lines[:self._max_output_lines])
        if len(lines) > self._max_output_lines:
            result += f"\n\n（结果已截断，共 {len(lines)} 行，仅显示前 {self._max_output_lines} 行）"
        return result

    def _visible_rg_lines(
        self, raw_lines: list[str], search_root: Path, session_id: str
    ) -> list[str]:
        """按可见性过滤 rg --null 输出，并还原成「相对路径:行号:内容」格式。

        rg 只认 .gitignore 与隐藏文件规则，部署目录不是 git 仓库时就会扫进 data/。
        这里逐行按会话可见性过滤，与 Python 回退分支的口径保持一致。
        """
        root = self._root.resolve()
        verdicts: dict[str, bool] = {}
        kept: list[str] = []
        for line in raw_lines:
            path_part, sep, rest = line.partition("\0")
            if not sep:
                # -C 分组分隔符 "--"：前面有保留下来的内容才留
                if kept and kept[-1] != "--":
                    kept.append(line)
                continue
            visible = verdicts.get(path_part)
            if visible is None:
                visible = _visible(root, Path(path_part), session_id, is_dir=False, resolve=True)
                verdicts[path_part] = visible
            if not visible:
                continue
            try:
                shown = str(Path(path_part).relative_to(search_root))
            except ValueError:
                shown = path_part
            # --null 把路径后的 ':'（匹配行）/ '-'（上下文行）换成了 NUL，这里还原
            marker = ":" if re.match(r"\d+:", rest) else "-"
            kept.append(f"{shown}{marker}{rest}")
        while kept and kept[-1] == "--":
            kept.pop()
        return kept

    def _grep_python(
        self, pattern: str, search_root: Path,
        glob_filter: str | None, case_insensitive: bool,
        max_results: int,
        *, session_id: str,
    ) -> str:
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"错误: 正则表达式无效: {exc}"

        results: list[str] = []
        count = 0
        root = self._root.resolve()

        for root_dir, dirs, files in os.walk(search_root):
            # 排除目录：常见噪音目录、点目录，以及对本会话不可见的目录（整棵剪掉）
            dirs[:] = [
                d for d in dirs
                if d not in _DEFAULT_EXCLUDES and not d.startswith(".")
                and _visible(root, Path(root_dir) / d, session_id, is_dir=True)
            ]

            for fname in sorted(files):
                if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
                    continue

                fpath = Path(root_dir) / fname
                if not _visible(root, fpath, session_id, is_dir=False):
                    continue
                if _is_binary(fpath):
                    continue

                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError):
                    continue

                for line_no, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        rel = str(fpath.relative_to(search_root))
                        results.append(f"{rel}:{line_no}:{line.rstrip()}")
                        count += 1
                        if count >= max_results:
                            break
                if count >= max_results:
                    break
            if count >= max_results:
                break

        if not results:
            return "未找到匹配结果。"

        output = "\n".join(results[:self._max_output_lines])
        if count >= max_results:
            output += f"\n\n（结果已截断，达到上限 {max_results} 条）"
        return output


class GlobTool(BaseTool):
    """按通配符匹配文件路径。"""

    name = "glob"
    description = "按通配符匹配文件路径，返回按修改时间倒序的文件列表。参数: pattern(通配符), path(搜索路径)"

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "通配符模式（如 '**/*.py'）"},
                "path": {"type": "string", "description": "搜索路径（相对工作区，默认 '.'）"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        pattern = str(arguments.get("pattern") or "").strip()
        if not pattern:
            return "错误: 缺少 pattern 参数"

        if any(part == ".." for part in Path(pattern).parts):
            return "错误: 通配符不得包含 .."

        user_path = str(arguments.get("path") or ".").strip()
        search_root = _resolve_search_root(self._root, user_path)
        if isinstance(search_root, str):
            return search_root
        root = self._root.resolve()
        if not _visible(root, search_root, scope.session_id, is_dir=search_root.is_dir()):
            return _OUT_OF_SCOPE

        matches: list[tuple[float, str]] = []
        for p in search_root.glob(pattern):
            if not p.is_file():
                continue
            if not _visible(root, p, scope.session_id, is_dir=False, resolve=True):
                continue
            if _should_exclude(p.relative_to(search_root), _DEFAULT_EXCLUDES):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            rel = str(p.relative_to(search_root)).replace("\\", "/")
            matches.append((mtime, rel))

        if not matches:
            return f"未找到匹配 '{pattern}' 的文件。"

        # 按修改时间倒序
        matches.sort(key=lambda x: x[0], reverse=True)
        lines = [m[1] for m in matches[:_MAX_OUTPUT_LINES]]
        result = "\n".join(lines)
        if len(matches) > _MAX_OUTPUT_LINES:
            result += f"\n\n（共 {len(matches)} 个文件，仅显示前 {_MAX_OUTPUT_LINES} 个）"
        return result


class SearchToolPack(ToolPack):
    """搜索工具包：grep + glob。"""

    pack_name = "search"
    description = "代码/文本搜索工具包"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        root_dir = str(config.get("root_dir") or ".")
        if Path(root_dir).is_absolute():
            root = Path(root_dir)
        else:
            root = PROJECT_ROOT / root_dir
        max_results = int(config.get("max_results") or 100)
        max_output_lines = int(config.get("max_output_lines") or 200)

        self._tools = [
            GrepTool(root, max_results=max_results, max_output_lines=max_output_lines),
            GlobTool(root),
        ]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
