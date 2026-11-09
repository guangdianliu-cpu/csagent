"""工作区文件工具包：在项目根（可配置）内 list / read / write / edit / send。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from csagent.channels.outbound_files import (
    OutboundFile,
    detect_media_type,
    enqueue_outbound_file,
)
from csagent.channels.workspace_uploads import (
    INBOUND_DIRNAME,
    bind_session_workspace_path,
    session_probe_candidates,
)
from csagent.common.secrets import get_secret_masker
from csagent.infra.config import PROJECT_ROOT
from csagent.memory.artifact_store import register_artifact
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.skill_paths import is_skill_path, resolve_skill_resource
from csagent.tools.visibility import DEFAULT_DENY_NAMES as _DEFAULT_DENY_NAMES
from csagent.tools.visibility import is_denied_path

logger = structlog.get_logger(__name__)

# _DEFAULT_DENY_NAMES / is_denied_path 的定义已移到 tools/visibility.py（grep/glob 共用），
# 这里保留同名导出，router_files / pdf_pack 等既有引用不用改。


def resolve_workspace_path(
    root: Path,
    relative: str,
    *,
    allow_absolute: bool = False,
) -> Path | str:
    """解析工作区内路径；越界或非法则返回错误文案。

    支持 <skill:name>/... 前缀：解析到 Skill 目录（只读）。
    """
    raw = str(relative or "").strip()
    if not raw or raw in {".", "./"}:
        return root.resolve()

    # Skill 路径解析
    if raw.startswith("<skill:"):
        return resolve_skill_resource(raw)

    path = Path(raw)
    if path.is_absolute():
        if not allow_absolute:
            return "错误: 默认只允许相对工作区根的路径；可设 allow_absolute: true"
        candidate = path.resolve()
    else:
        candidate = (root / raw).resolve()

    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return f"错误: 路径越出工作区 root={root}"
    return candidate


def _send_file_hint(rel_path: str, *, send_enabled: bool = True) -> str:
    """写在 outbound 时，提醒可接 send_file（send_enabled=false 时不提示）。"""
    if not send_enabled:
        return ""
    normalized = rel_path.replace("\\", "/")
    # 不提示 send 用户上传目录（inbound）里的原件
    if f"/{INBOUND_DIRNAME}/" in f"/{normalized}/" or normalized.endswith(
        f"/{INBOUND_DIRNAME}"
    ):
        return ""
    if normalized.startswith("data/uploads/") or "/data/uploads/" in f"/{normalized}":
        return f'\n仅当用户明确要求发回时，可调用 send_file({{"path":"{normalized}"}})。'
    return ""


def _tool_rel_path(
    relative: str,
    scope: CustomerScope,
    *,
    session_scoped: bool,
    for_write: bool,
    prefer_outbound: bool = False,
    probe_existing: bool | None = None,
    project_root: Path | None = None,
) -> str:
    raw = str(relative or "").strip()
    if is_skill_path(raw):
        # Skill 路径不是会话相对路径，绕开 session 绑定，原样交给 resolve_workspace_path
        return raw
    if not session_scoped:
        return raw or "."
    return bind_session_workspace_path(
        relative,
        scope.session_id,
        for_write=for_write,
        prefer_outbound=prefer_outbound,
        probe_existing=probe_existing,
        # 探测已存在文件必须与工具自身的 root 一致；否则根路径不同时永远探测不到，
        # 退化成默认落点。
        project_root=project_root,
    )


def _not_found_message(
    raw: str,
    rel: str,
    scope: CustomerScope,
    *,
    session_scoped: bool,
    prefer_outbound: bool = False,
) -> str:
    """「文件不存在」附带实际探测过的位置。

    会话相对路径会被绑定到 uploads/ 或 scripts/ 下的具体位置，模型看不到这层改写；
    只回绑定后的路径会让它无从修正，进而反复重写文件（见 audit wecom_BoWen）。
    """
    # skills/<name>/... 已在绑定时改写成 Skill 路径，会话探测位置与它无关
    if not session_scoped or is_skill_path(raw) or is_skill_path(rel):
        return f"错误: 文件不存在: {rel}"
    probed = session_probe_candidates(raw, scope.session_id, prefer_outbound=prefer_outbound)
    if not probed:
        return f"错误: 文件不存在: {rel}"
    listed = "\n".join(f"- {p}" for p in probed)
    return (
        f"错误: 文件不存在: {rel}\n"
        f"已在以下位置查找 {raw!r}：\n{listed}\n"
        "请用 list_dir 确认实际路径，或改用完整的工作区相对路径。"
    )


def _reject_dotdot(rel: str) -> str | None:
    if any(part == ".." for part in Path(rel.replace("\\", "/")).parts):
        return "错误: 路径不得包含 .."
    return None


class ListDirTool(BaseTool):
    """列出工作区目录内容。"""

    name = "list_dir"
    description = (
        "列出工作区目录。参数: path(默认当前会话 data/uploads/{session}/；"
        "Skill 目录用 skills/<name>/ 或 <skill:name>；会话脚本在 scripts/tools/)。"
        "max_entries(可选，默认 200)。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作区路径，默认当前会话目录",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "最多列出条目数，默认 200",
                },
            },
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        max_entries: int = 200,
        deny_names: set[str] | None = None,
        session_scoped: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._max_entries = max(1, min(max_entries, 2000))
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._session_scoped = session_scoped

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        rel = _tool_rel_path(
            str(arguments.get("path") or "."),
            scope,
            session_scoped=self._session_scoped,
            for_write=False,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return err
        limit = arguments.get("max_entries")
        try:
            max_entries = int(limit) if limit is not None else self._max_entries
        except (TypeError, ValueError):
            max_entries = self._max_entries
        max_entries = max(1, min(max_entries, 2000))

        target = resolve_workspace_path(
            self._root, rel, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"错误: 路径不存在: {rel}"
        if not target.is_dir():
            return f"错误: 不是目录: {rel}"

        entries: list[str] = []
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return f"错误: 无法列出目录: {exc}"

        for child in children:
            if is_denied_path(child, self._deny_names):
                continue
            kind = "dir" if child.is_dir() else "file"
            rel_name = child.relative_to(self._root.resolve()).as_posix()
            if kind == "file":
                try:
                    size = child.stat().st_size
                except OSError:
                    size = -1
                entries.append(f"[{kind}] {rel_name} ({size} bytes)")
            else:
                entries.append(f"[{kind}] {rel_name}/")
            if len(entries) >= max_entries:
                entries.append(f"... 已截断，最多显示 {max_entries} 条")
                break

        if not entries:
            return f"目录为空: {rel}"
        return "\n".join([f"工作区目录: {rel}", *entries])


class ReadFileTool(BaseTool):
    """读取工作区文本文件。"""

    name = "read_file"
    description = (
        "读取工作区文本文件。参数 path 必填；大文件必须用 offset(1-based 起始行)+limit(行数) 分段读，"
        "建议 limit=600，下一页 offset=上一页终点+1。"
        "超长结果会被外置为 artifact（read_artifact 按页取回），不要一次不带 limit 读整文件。"
        "默认相对当前会话 data/uploads/{session}/，也可读 outbound/ 与 scripts/ 等项目路径。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作区的文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（1-based）。大文件必填；下一页=上一页结束行号+1",
                },
                "limit": {
                    "type": "integer",
                    "description": "本段最多读取行数。大文件必填，建议 600",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        max_chars: int = 100_000,
        deny_names: set[str] | None = None,
        session_scoped: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._max_chars = max(1000, max_chars)
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._session_scoped = session_scoped

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return "错误: 缺少参数 path"
        rel = _tool_rel_path(
            raw,
            scope,
            session_scoped=self._session_scoped,
            for_write=False,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return err

        target = resolve_workspace_path(
            self._root, rel, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if is_denied_path(target, self._deny_names):
            return f"错误: 出于安全限制，拒绝读取敏感文件: {target.name}"
        if not target.is_file():
            return _not_found_message(
                raw, rel, scope, session_scoped=self._session_scoped
            )

        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"错误: 文件不是 UTF-8 文本，无法读取: {rel}"
        except OSError as exc:
            return f"错误: 读取失败: {exc}"

        lines = text.splitlines(keepends=True)
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        start = 0
        if offset is not None:
            try:
                start = max(0, int(offset) - 1)
            except (TypeError, ValueError):
                start = 0
        end = len(lines)
        if limit is not None:
            try:
                end = min(len(lines), start + max(1, int(limit)))
            except (TypeError, ValueError):
                end = len(lines)

        slice_lines = lines[start:end]
        body = "".join(slice_lines)
        truncated = False
        if len(body) > self._max_chars:
            body = body[: self._max_chars]
            truncated = True

        header = (
            f"文件: {rel} (行 {start + 1}-{start + len(slice_lines)} / 共 {len(lines)} 行)"
        )
        if truncated:
            header += f" [内容截断至 {self._max_chars} 字符]"
            header += (
                f"；请改用更小的 limit 分段读，例如 "
                f"offset={start + 1}, limit=600，再继续下一页"
            )
        elif end < len(lines):
            header += f"；还有后续，下一页 offset={end + 1}"
        return f"{header}\n\n{body}" if body else f"{header}\n\n(空文件)"


class WriteFileTool(BaseTool):
    """写入或覆盖工作区文本文件。"""

    name = "write_file"
    description = (
        "写入文本文件（覆盖）。参数: path/content；"
        "默认落到 data/uploads/{session}/outbound/，自动创建父目录。"
        "用户上传文件在 inbound/，不要往 inbound 写。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "content": {"type": "string", "description": "文件全文内容"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        max_chars: int = 200_000,
        deny_names: set[str] | None = None,
        session_scoped: bool = True,
        send_enabled: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._max_chars = max(1000, max_chars)
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._session_scoped = session_scoped
        self._send_enabled = send_enabled

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return "错误: 缺少参数 path"
        # Skill 目录只读保护
        if is_skill_path(raw):
            return "错误: Skill 目录为只读，不允许写入"
        if "content" not in arguments:
            return "错误: 缺少参数 content"
        content = arguments.get("content")
        content_str = content if isinstance(content, str) else str(content)
        if len(content_str) > self._max_chars:
            return f"错误: content 超过上限 {self._max_chars} 字符"

        rel = _tool_rel_path(
            raw,
            scope,
            session_scoped=self._session_scoped,
            for_write=True,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return err

        target = resolve_workspace_path(
            self._root, rel, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if target.exists() and target.is_dir():
            return f"错误: 目标是目录，不能写入: {rel}"
        if is_denied_path(target, self._deny_names):
            return f"错误: 出于安全限制，拒绝写入敏感文件: {target.name}"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content_str.encode("utf-8")
            target.write_bytes(data)
            rel_out = target.relative_to(self._root.resolve()).as_posix()
            from csagent.audit.evidence import record_bytes

            record_bytes("write_file", data, path=rel_out)
            return f"已写入: {rel_out}（{len(data)} bytes）{_send_file_hint(rel_out, send_enabled=self._send_enabled)}"
        except OSError as exc:
            logger.warning("write_file_failed", path=rel, error=str(exc))
            return f"写入失败: {exc}"


class EditFileTool(BaseTool):
    """按精确字符串替换编辑工作区文件。"""

    name = "edit_file"
    description = (
        "编辑文本文件。参数: path/old_string/new_string；可选 replace_all。"
        "默认相对当前会话 data/uploads/{session}/。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "old_string": {"type": "string", "description": "要替换的原文（精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的文本"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换全部匹配，默认 false",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        max_chars: int = 200_000,
        deny_names: set[str] | None = None,
        session_scoped: bool = True,
        send_enabled: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._max_chars = max(1000, max_chars)
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._session_scoped = session_scoped
        self._send_enabled = send_enabled

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        raw = str(arguments.get("path") or "").strip()
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not raw:
            return "错误: 缺少参数 path"
        # Skill 目录只读保护
        if is_skill_path(raw):
            return "错误: Skill 目录为只读，不允许编辑"
        if old is None:
            return "错误: 缺少参数 old_string"
        if new is None:
            return "错误: 缺少参数 new_string"
        old_str = old if isinstance(old, str) else str(old)
        new_str = new if isinstance(new, str) else str(new)
        if not old_str:
            return "错误: old_string 不能为空"
        replace_all = bool(arguments.get("replace_all", False))

        rel = _tool_rel_path(
            raw,
            scope,
            session_scoped=self._session_scoped,
            for_write=True,
            # 编辑的目标必须已存在：先在会话的 uploads/ 与 scripts/ 两棵树里找，
            # 找不到才回落到 outbound 默认落点（用于给出可读的错误）。
            probe_existing=True,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return err

        target = resolve_workspace_path(
            self._root, rel, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if is_denied_path(target, self._deny_names):
            return f"错误: 出于安全限制，拒绝编辑敏感文件: {target.name}"
        if not target.is_file():
            return _not_found_message(
                raw, rel, scope, session_scoped=self._session_scoped
            )

        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"错误: 文件不是 UTF-8 文本: {rel}"
        except OSError as exc:
            return f"错误: 读取失败: {exc}"

        count = text.count(old_str)
        if count == 0:
            return "错误: old_string 在文件中未找到（须精确匹配，含空白/缩进）"
        if count > 1 and not replace_all:
            return (
                f"错误: old_string 匹配到 {count} 处；"
                "请提供更唯一的上下文，或设 replace_all=true"
            )

        updated = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        if len(updated) > self._max_chars:
            return f"错误: 编辑后内容超过上限 {self._max_chars} 字符"

        try:
            target.write_text(updated, encoding="utf-8")
            rel_out = target.relative_to(self._root.resolve()).as_posix()
            from csagent.audit.evidence import record_path

            record_path("edit_file", target, copy=True, path=rel_out)
            replaced = count if replace_all else 1
            return f"已编辑: {rel_out}（替换 {replaced} 处）{_send_file_hint(rel_out, send_enabled=self._send_enabled)}"
        except OSError as exc:
            logger.warning("edit_file_failed", path=rel, error=str(exc))
            return f"编辑失败: {exc}"


class SendFileTool(BaseTool):
    """将工作区文件加入出站队列，由通道在本轮回复结束后发给用户。"""

    name = "send_file"
    description = (
        "把文件作为附件发回用户。仅当用户本轮明确要求发送/导出文件时调用；"
        "禁止因历史发过文件或结果很多就自行发送。"
        "参数: path（默认相对当前会话 "
        "data/uploads/{session}/outbound/；裸文件名优先找 outbound 再找 inbound）；"
        "成功后由通道在本轮文字回复结束后发出。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要发送的文件路径"},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        deny_names: set[str] | None = None,
        send_enabled: bool = True,
        session_scoped: bool = True,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._send_enabled = send_enabled
        self._session_scoped = session_scoped

    def _resolve(self, raw: str, scope: CustomerScope) -> tuple[str, Path | str]:
        """``(绑定会话后的相对路径, 解析结果)``；解析结果是错误文案时为 str。"""
        rel = _tool_rel_path(
            raw,
            scope,
            session_scoped=self._session_scoped,
            for_write=False,
            prefer_outbound=True,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return rel, err
        return rel, resolve_workspace_path(self._root, rel, allow_absolute=self._allow_absolute)

    def approval_identity(self, arguments: dict[str, Any], scope: CustomerScope) -> dict[str, Any]:
        """``outbound/a.txt`` 与 ``data/uploads/<会话>/outbound/a.txt`` 是同一个文件、同一张卡片。"""
        raw = str(arguments.get("path") or "").strip()
        _, target = self._resolve(raw, scope) if raw else ("", "")
        if isinstance(target, str):
            return arguments
        return {"path": target.as_posix()}

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        if not self._send_enabled:
            return "错误: 出站发文件未启用（tool_packs.yaml send_enabled=false）"
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return "错误: 缺少参数 path"

        rel, target = self._resolve(raw, scope)
        if isinstance(target, str):
            return target
        if is_denied_path(target, self._deny_names):
            return f"错误: 出于安全限制，拒绝发送敏感文件: {target.name}"
        if not target.is_file():
            return _not_found_message(
                raw, rel, scope, session_scoped=self._session_scoped, prefer_outbound=True
            )

        try:
            rel_out = target.relative_to(self._root.resolve()).as_posix()
        except ValueError:
            rel_out = str(target)
        # 外发前扫一遍已知密钥值：脚本可以不经模型直接把密钥写进文件，回复正文的
        # 脱敏管不到这条路
        if get_secret_masker().file_contains_secret(target):
            logger.error("send_file_secret_blocked", path=rel_out, session_id=scope.session_id)
            return f"错误: 出于安全限制，拒绝发送 {target.name}：文件内容包含受保护的密钥。"
        media_type = detect_media_type(target.name)
        queue_size = enqueue_outbound_file(
            scope.session_id,
            OutboundFile(
                path=str(target.resolve()),
                workspace_path=rel_out,
                filename=target.name,
                media_type=media_type,
            ),
        )
        # 入队是本轮内的交接（通道马上取走），登记是永久清单——两件事，都要做。
        # 登记失败只告警：文件已经入队了，不能因为清单没记上就让发送失败。
        register_artifact(
            session_id=scope.session_id,
            kind="outbound",
            path=rel_out,
            filename=target.name,
        )
        from csagent.audit.evidence import record_path

        # 外发文件可能很大，只记摘要和接收方，不把文件再拷一份进审计目录
        record_path(
            "send_file",
            target,
            copy=False,
            path=rel_out,
            filename=target.name,
            recipient={
                "user_id": scope.user_id,
                "chat_type": scope.chat_type,
                "chat_id": scope.chat_id,
            },
        )
        return (
            f"已加入出站队列: {rel_out}（类型={media_type}，队列={queue_size}）。"
            "通道将在本轮文字回复结束后发送给用户。"
        )


class WorkspaceFilesToolPack(ToolPack):
    """工作区级文件读写与出站发送工具包。"""

    pack_name = "workspace_files"
    description = "工作区文件 list/read/write/edit/send（默认按会话目录隔离）"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []
        self.root: Path | None = None

    def initialize(self, config: dict[str, Any]) -> None:
        root = Path(str(config.get("root_dir") or "."))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root

        allow_absolute = bool(config.get("allow_absolute", False))
        session_scoped = bool(config.get("session_scoped", True))
        max_read_chars = int(config.get("max_read_chars") or 100_000)
        max_write_chars = int(config.get("max_write_chars") or 200_000)
        max_entries = int(config.get("max_list_entries") or 200)
        send_enabled = bool(config.get("send_enabled", True))
        deny_raw = config.get("deny_names")
        if isinstance(deny_raw, list) and deny_raw:
            deny_names = {str(x).strip() for x in deny_raw if str(x).strip()}
        else:
            deny_names = set(_DEFAULT_DENY_NAMES)

        tools: list[BaseTool] = [
            ListDirTool(
                root,
                allow_absolute=allow_absolute,
                max_entries=max_entries,
                deny_names=deny_names,
                session_scoped=session_scoped,
            ),
            ReadFileTool(
                root,
                allow_absolute=allow_absolute,
                max_chars=max_read_chars,
                deny_names=deny_names,
                session_scoped=session_scoped,
            ),
            WriteFileTool(
                root,
                allow_absolute=allow_absolute,
                max_chars=max_write_chars,
                deny_names=deny_names,
                session_scoped=session_scoped,
                send_enabled=send_enabled,
            ),
            EditFileTool(
                root,
                allow_absolute=allow_absolute,
                max_chars=max_write_chars,
                deny_names=deny_names,
                session_scoped=session_scoped,
                send_enabled=send_enabled,
            ),
        ]
        if send_enabled:
            tools.append(
                SendFileTool(
                    root,
                    allow_absolute=allow_absolute,
                    deny_names=deny_names,
                    send_enabled=send_enabled,
                    session_scoped=session_scoped,
                )
            )

        desc_by_name = {
            str(item.get("name")): str(item.get("description") or "")
            for item in (config.get("tools") or [])
            if isinstance(item, dict) and item.get("name")
        }
        for tool in tools:
            if desc_by_name.get(tool.name):
                tool.description = desc_by_name[tool.name]

        self._tools = tools
        logger.info(
            "workspace_files_pack_ready",
            root=str(root),
            tools=[t.name for t in tools],
            allow_absolute=allow_absolute,
            session_scoped=session_scoped,
            send_enabled=send_enabled,
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
