"""用户附件暂存到工作区，供 list_dir / read_file / edit_file 访问。

约定目录（相对项目根）：
  data/uploads/{session_id}/inbound/   — 仅用户上传
  data/uploads/{session_id}/outbound/  — Agent 产出（交付物、payload 等）
  data/uploads/{session_id}/.active_inbound — 最近一次用户上传路径（单行）
  scripts/tools/{session_id}/          — Agent 写的会话脚本

会话工作区横跨 uploads/ 与 scripts/ 两棵树，但对模型只呈现为「本会话的文件」。
模型给的相对路径无法自带树的归属，故读取类操作按 ``session_probe_candidates``
的顺序在两棵树里探测已存在的文件；都没有才回落到默认落点。写入类操作不探测，
始终落到确定的默认位置，避免 write_file 意外改写脚本。

会话根下的旧扁平文件仍可读，不做迁移。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import structlog

from csagent.channels.models import Attachment
from csagent.infra.config import PROJECT_ROOT

logger = structlog.get_logger(__name__)

DEFAULT_UPLOADS_ROOT = PROJECT_ROOT / "data" / "uploads"
INBOUND_DIRNAME = "inbound"
OUTBOUND_DIRNAME = "outbound"
ACTIVE_INBOUND_NAME = ".active_inbound"
# 会话脚本树的根（相对项目根）；与 tool_packs.yaml 的 script_executor.scripts_dir 对齐。
SCRIPTS_ROOT_REL = "scripts/tools"

_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9._-]+")
_UNSAFE_FILENAME = re.compile(r"[^\w.\u4e00-\u9fff-]+", re.UNICODE)


def safe_session_segment(session_id: str) -> str:
    return _SAFE_SEGMENT.sub("_", (session_id or "unknown").strip())[:120] or "unknown"


def _strip_dot_slash(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def safe_filename(filename: str | None, *, index: int = 0, fallback_prefix: str = "file") -> str:
    name = Path((filename or "").strip()).name
    if not name or name in {".", ".."}:
        return f"{fallback_prefix}_{index}"
    cleaned = _UNSAFE_FILENAME.sub("_", name).strip("._") or f"{fallback_prefix}_{index}"
    return cleaned[:180]


def upload_dir_for_session(session_id: str, *, root: Path | None = None) -> Path:
    """会话根目录（兼容旧代码）；新逻辑请用 inbound/outbound helper。"""
    base = root or DEFAULT_UPLOADS_ROOT
    path = base / safe_session_segment(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_uploads_rel(session_id: str) -> str:
    """相对项目根的会话上传目录：data/uploads/{session}/。"""
    return f"data/uploads/{safe_session_segment(session_id)}"


def session_inbound_rel(session_id: str) -> str:
    return f"{session_uploads_rel(session_id)}/{INBOUND_DIRNAME}"


def session_outbound_rel(session_id: str) -> str:
    return f"{session_uploads_rel(session_id)}/{OUTBOUND_DIRNAME}"


def session_scripts_rel(session_id: str) -> str:
    """相对项目根的会话脚本目录：scripts/tools/{session}/。"""
    return f"{SCRIPTS_ROOT_REL}/{safe_session_segment(session_id)}"


def session_inbound_dir(session_id: str, *, root: Path | None = None) -> Path:
    path = upload_dir_for_session(session_id, root=root) / INBOUND_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_outbound_dir(session_id: str, *, root: Path | None = None) -> Path:
    path = upload_dir_for_session(session_id, root=root) / OUTBOUND_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def active_inbound_marker_path(session_id: str, *, root: Path | None = None) -> Path:
    return upload_dir_for_session(session_id, root=root) / ACTIVE_INBOUND_NAME


def set_active_inbound(
    session_id: str,
    workspace_path: str,
    *,
    root: Path | None = None,
) -> None:
    """记录最近一次用户上传的工作区相对路径。"""
    rel = _strip_dot_slash(workspace_path)
    if not rel:
        return
    marker = active_inbound_marker_path(session_id, root=root)
    try:
        marker.write_text(rel + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "active_inbound_write_failed",
            session_id=session_id,
            path=rel,
            error=str(exc),
        )


def get_active_inbound(
    session_id: str,
    *,
    root: Path | None = None,
    project_root: Path | None = None,
) -> str | None:
    """读取最近用户上传路径；文件已不存在则返回 None。"""
    marker = active_inbound_marker_path(session_id, root=root)
    if not marker.is_file():
        return None
    try:
        rel = _strip_dot_slash(
            marker.read_text(encoding="utf-8").splitlines()[0]
            if marker.stat().st_size
            else ""
        )
    except OSError:
        return None
    if not rel:
        return None
    abs_root = (project_root or PROJECT_ROOT).resolve()
    candidate = (abs_root / rel).resolve()
    try:
        candidate.relative_to(abs_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return rel


# ---------------------------------------------------------------- 读路径放行
#
# 读类操作（read_file / list_dir / send_file 等）默认被绑到本会话目录。只有两类
# 项目路径可以显式访问，且都要先改写到受控的解析口径：
#
# - ``skills/<name>/...`` → ``<skill:name>/...``：走 Skill 解析，当前形态白名单外、
#   未启用的 Skill 一律拒绝。以前按原样放行，一个形态可以把另一个形态 Skill 里的
#   数据库文件整个读出、发出去。
#   glob 常返回 skills/...（见 audit b1d42d05），所以前缀本身要认。
# - ``scripts/tools/...``：根下单文件是共享脚本，原样放行；带子目录的一律改写到
#   本会话的脚本目录，读不到别的会话写的脚本。
#
# 源码、配置、文档、审计日志不再放行（以前任何会话都能 read_file 读到全量审计）。
# 调试部署确需让模型读这些目录时，在 configs/base.yaml 的
# ``workspace.read_escape_prefixes`` 显式列出，由 main.py 启动时注入。
_read_escape_prefixes: tuple[str, ...] = ()


def configure_read_escape_prefixes(prefixes: list[str] | tuple[str, ...] | None) -> None:
    """设置额外放行的读路径前缀（相对项目根，如 ``docs/``）；None / 空 = 不放行。

    统一补成以 ``/`` 结尾，避免 ``doc`` 误放行 ``docs_private/``。
    """
    global _read_escape_prefixes
    normalized: list[str] = []
    for raw in prefixes or ():
        value = _strip_dot_slash(str(raw)).strip().strip("/")
        if value and ".." not in value.split("/"):
            normalized.append(value + "/")
    _read_escape_prefixes = tuple(normalized)


def read_escape_prefixes() -> tuple[str, ...]:
    """当前生效的额外放行前缀（只读）。"""
    return _read_escape_prefixes


def _bind_read_escape(raw: str, seg: str) -> str | None:
    """读类路径若属于可显式访问的项目路径，返回改写后的路径；否则 None。"""
    if raw.startswith("skills/"):
        name, _, rest = raw[len("skills/") :].partition("/")
        if not name:
            return None
        return f"<skill:{name}>/{rest}" if rest else f"<skill:{name}>"
    if raw == SCRIPTS_ROOT_REL:
        return f"{SCRIPTS_ROOT_REL}/{seg}"
    if raw.startswith(f"{SCRIPTS_ROOT_REL}/"):
        _session_dir, sep, rest = raw[len(SCRIPTS_ROOT_REL) + 1 :].partition("/")
        if not sep:
            return raw  # scripts/tools/x.py：共享脚本
        # 不论原路径写的是哪个会话，都只落到本会话
        return f"{SCRIPTS_ROOT_REL}/{seg}/{rest}"
    for prefix in _read_escape_prefixes:
        if raw == prefix.rstrip("/") or raw.startswith(prefix):
            return raw
    return None


def _is_bare_name(raw: str) -> bool:
    """无目录分隔的文件名（可含中文）。"""
    return bool(raw) and "/" not in raw and raw not in {".", "./"}


def _existing_under(project_root: Path, rel: str) -> bool:
    try:
        return (project_root / rel).is_file()
    except OSError:
        return False


def session_probe_candidates(
    relative: str,
    session_id: str,
    *,
    prefer_outbound: bool = False,
) -> list[str]:
    """一个会话相对路径可能对应的所有落点（相对项目根，按探测优先级排序）。

    会话工作区横跨 ``data/uploads/{session}/``（附件与交付物）和
    ``scripts/tools/{session}/``（会话脚本）两棵树。模型写 ``foo.py`` 或
    ``{session}/foo.py`` 时并不区分树，故两棵都要探测——否则 write_script 刚写出的
    脚本，read_file/edit_file 永远找不到（会被绑到 uploads 下报「文件不存在」）。

    调用方用于：按顺序找已存在的文件；找不到时把本列表回给模型说明找过哪些位置。
    """
    seg = safe_session_segment(session_id)
    base = f"data/uploads/{seg}"
    inbound = f"{base}/{INBOUND_DIRNAME}"
    outbound = f"{base}/{OUTBOUND_DIRNAME}"
    raw = _strip_dot_slash(relative).lstrip("/")
    if not raw or raw in {".", "./"}:
        return []

    parents = [outbound, inbound, base] if prefer_outbound else [inbound, outbound, base]
    candidates = [f"{parent}/{raw}" for parent in parents]
    # 脚本树按 scripts 自身的会话绑定语义解析（raw 可能已带 {session}/ 前缀）
    candidates.append(f"{SCRIPTS_ROOT_REL}/{bind_session_script_path(raw, session_id)}")

    seen: set[str] = set()
    ordered: list[str] = []
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    return ordered


def bind_session_workspace_path(
    relative: str,
    session_id: str,
    *,
    for_write: bool = False,
    prefer_outbound: bool = False,
    probe_existing: bool | None = None,
    project_root: Path | None = None,
) -> str:
    """将工具 path 绑定到当前会话目录。

    - 空 / ``.`` → 会话根（list_dir 用）；写入时落到 outbound/
    - 已在 ``data/uploads/{当前会话}/`` 下 → 原样（含 inbound/outbound）
    - 在 ``data/uploads/`` 其它会话下 → 改写到当前会话（保留 inbound/outbound 后缀若有）
    - ``for_write=False`` 时，``skills/<name>/`` 改写为 ``<skill:name>/``、
      ``scripts/tools/`` 只放行共享脚本与本会话子目录，其余项目路径须在
      ``workspace.read_escape_prefixes`` 显式放行（见 ``_bind_read_escape``）
    - ``for_write=True`` 时强制写入当前会话（默认 outbound）

    ``probe_existing``（默认 ``not for_write``）为真时，先按
    ``session_probe_candidates`` 在 uploads/ 与 scripts/ 两棵树里找已存在的文件；
    都没有才用默认落点。edit_file 这类「改已存在的文件」要显式传 True——它
    ``for_write=True``（决定回落落点），但目标必须先被找到。
    """
    seg = safe_session_segment(session_id)
    base = f"data/uploads/{seg}"
    inbound = f"{base}/{INBOUND_DIRNAME}"
    outbound = f"{base}/{OUTBOUND_DIRNAME}"
    root = (project_root or PROJECT_ROOT).resolve()

    raw_input = str(relative or "").strip()
    path_obj = Path(raw_input)
    if path_obj.is_absolute():
        try:
            raw = path_obj.resolve().relative_to(root).as_posix()
        except ValueError:
            # 越出 root 的绝对路径保持原样，交由上层安全校验拦截
            return raw_input
    else:
        raw = _strip_dot_slash(raw_input)

    if probe_existing is None:
        probe_existing = not for_write

    if not raw or raw in {".", "./"}:
        return outbound if for_write else base

    if raw.startswith(f"{base}/") or raw == base:
        return raw

    if raw.startswith("data/uploads/"):
        rest = raw[len("data/uploads/") :]
        parts = rest.split("/", 1)
        if len(parts) == 2:
            # other_session/inbound/x → current/inbound/x
            return f"{base}/{parts[1]}"
        return f"{base}/{parts[0]}"

    if not for_write:
        escaped = _bind_read_escape(raw, seg)
        if escaped is not None:
            return escaped

    # 显式 inbound/xxx 或 outbound/xxx（相对会话）
    if raw.startswith(f"{INBOUND_DIRNAME}/") or raw.startswith(f"{OUTBOUND_DIRNAME}/"):
        return f"{base}/{raw}"

    if probe_existing:
        for candidate in session_probe_candidates(
            raw, session_id, prefer_outbound=prefer_outbound
        ):
            if _existing_under(root, candidate):
                return candidate

    if _is_bare_name(raw):
        if for_write:
            return f"{outbound}/{raw}"
        # 默认落点：读偏 inbound，发偏 outbound
        return f"{outbound}/{raw}" if prefer_outbound else f"{inbound}/{raw}"

    # 其它相对路径：写入进 outbound，读取进会话根（兼容旧扁平文件）
    if for_write:
        return f"{outbound}/{raw.lstrip('/')}"
    return f"{base}/{raw.lstrip('/')}"


def bind_session_script_path(relative: str, session_id: str) -> str:
    """将脚本 path 绑定到 scripts_dir 下的会话子目录：{session}/{path}。"""
    seg = safe_session_segment(session_id)
    raw = _strip_dot_slash(relative)
    if not raw or raw in {".", "./"}:
        return seg
    if raw == seg or raw.startswith(f"{seg}/"):
        return raw
    return f"{seg}/{raw.lstrip('/')}"


def workspace_relative(path: Path, *, project_root: Path | None = None) -> str:
    root = (project_root or PROJECT_ROOT).resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def stage_attachment(
    attachment: Attachment,
    *,
    session_id: str,
    index: int = 0,
    uploads_root: Path | None = None,
    project_root: Path | None = None,
    update_pointer: bool = True,
) -> str | None:
    """将附件落到 data/uploads/{session}/inbound/，并更新 .active_inbound。

    - 已有 local_path：复制到 inbound（已在目标目录则原地复用，不再复制副本）
    - 无 local_path：无法暂存，返回 None
    - ``update_pointer=False``：只暂存，不抢「当前用户文件」指针（引用里的旧附件）
    成功后会更新 attachment.local_path，并写入 metadata.workspace_path。
    """
    root = project_root or PROJECT_ROOT
    dest_dir = session_inbound_dir(session_id, root=uploads_root or DEFAULT_UPLOADS_ROOT)
    local = (attachment.local_path or "").strip()
    if not local:
        logger.info(
            "workspace_upload_skip_no_local",
            session_id=session_id,
            filename=attachment.filename,
        )
        return None

    src = Path(local)
    if not src.is_file():
        logger.warning(
            "workspace_upload_missing_source",
            session_id=session_id,
            path=local,
        )
        return None

    fname = safe_filename(
        attachment.filename or src.name,
        index=index,
        fallback_prefix=attachment.type.value if hasattr(attachment.type, "value") else "file",
    )
    # 源已在 inbound 内（企微下载、HTTP 上传都直接落这里）→ 原地复用。
    # 否则目标名按 attachment.filename 重算，与落盘时去重产生的 x_1 对不上，
    # 每次入站都会再复制一份副本（x_2、x_3…）。
    if src.parent.resolve() == dest_dir.resolve():
        dest = src
    else:
        dest = dest_dir / fname
        # 同名冲突时加序号，避免覆盖
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            n = 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{n}{suffix}"
                n += 1

    try:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        rel = workspace_relative(dest, project_root=root)
        attachment.local_path = str(dest.resolve())
        attachment.metadata["workspace_path"] = rel
        attachment.metadata["file_role"] = "inbound"
        attachment.filename = attachment.filename or fname
        if update_pointer:
            set_active_inbound(session_id, rel, root=uploads_root or DEFAULT_UPLOADS_ROOT)
        logger.info(
            "workspace_upload_staged",
            session_id=session_id,
            workspace_path=rel,
            bytes=dest.stat().st_size,
            pointer_updated=update_pointer,
        )
        return rel
    except OSError as exc:
        logger.warning(
            "workspace_upload_failed",
            session_id=session_id,
            path=local,
            error=str(exc),
        )
        return None
