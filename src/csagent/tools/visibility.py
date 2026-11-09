"""读类工具的可见性边界：某个工作区路径对当前会话是否可见。

read_file / list_dir / send_file 经 ``bind_session_workspace_path`` 把路径绑到会话目录，
grep / glob 则从工作区根往下扫，两条路必须对「能看见什么」给出同一个答案——
否则模型读不到的文件，换 grep 就能把内容逐行打出来。本模块是这条边界的唯一定义点，
被 workspace_files_pack 与 search_pack 共用，避免两个 ToolPack 互相 import。

可见范围（相对工作区根）：

- 本会话目录：``data/uploads/{session}/``、``scripts/tools/{session}/``
- ``scripts/tools/`` 根下的共享脚本文件
- 当前形态白名单内的 Skill：``skills/<name>/``（经 Skill 解析，未启用即不可见）
- ``workspace.read_escape_prefixes`` 显式放行的前缀（默认无，仅调试部署按需打开）
- 以上之外、且不属于运行时数据与项目内部目录的普通文件

任何一段以 ``.`` 开头（``.env*``、``.git``……）或命中敏感文件名的路径一律不可见。
"""

from __future__ import annotations

from pathlib import Path

from csagent.channels.workspace_uploads import (
    SCRIPTS_ROOT_REL,
    read_escape_prefixes,
    safe_session_segment,
)
from csagent.tools.skill_paths import resolve_skill_resource

# 敏感文件名（小写比较）：读、写、发送、搜索一律拒绝
DEFAULT_DENY_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)

# 运行时数据根：只放行本会话的上传目录，审计 / 记忆库 / LLM 原文 / 日志都不可见
_RUNTIME_DATA_DIR = "data"
# 项目内部目录：源码、配置、文档、测试，不是任何会话的工作资料
_INTERNAL_TOP_DIRS = frozenset({"configs", "src", "docs", "services", "tests", "repos"})


def is_denied_name(name: str, deny_names: set[str] | frozenset[str] | None = None) -> bool:
    """按文件名判定是否为敏感文件（密钥、凭证、按形态拆分的 env 文件）。"""
    lowered = name.lower()
    names = deny_names if deny_names is not None else DEFAULT_DENY_NAMES
    if lowered in {n.lower() for n in names}:
        return True
    # .env.research / .env.product 等按形态拆分的实例配置同样含密钥
    if lowered.startswith(".env.") and lowered != ".env.example":
        return True
    # 常见密钥后缀
    if lowered.endswith((".pem", ".key")) and lowered not in {"public.key"}:
        return True
    return False


def is_denied_path(path: Path, deny_names: set[str] | frozenset[str] | None = None) -> bool:
    """路径的文件名是否敏感；``deny_names`` 缺省用 :data:`DEFAULT_DENY_NAMES`。"""
    return is_denied_name(path.name, deny_names)


def _matches_escape_prefix(rel: str) -> bool:
    for prefix in read_escape_prefixes():
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return True
    return False


def _skill_visible(name: str) -> bool:
    """Skill 是否对当前形态可见：未注册、未启用、Skill 系统未初始化都算不可见。"""
    return not isinstance(resolve_skill_resource(f"<skill:{name}>"), str)


def is_visible_to_session(rel: str, session_id: str, *, is_dir: bool = False) -> bool:
    """``rel``（相对工作区根）对 ``session_id`` 是否可见。

    :param rel: 相对工作区根的路径，正反斜杠均可
    :param session_id: 当前会话
    :param is_dir: ``rel`` 是否为目录。``data/``、``scripts/tools/`` 这类目录本身只是
        通往本会话目录的路径，可以穿过，但其下的文件不放出
    :returns: True 表示可见
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return True
    if any(p.startswith(".") for p in parts):
        return False
    if not is_dir and is_denied_name(parts[-1]):
        return False
    if _matches_escape_prefix("/".join(parts) + ("/" if is_dir else "")):
        return True

    seg = safe_session_segment(session_id)
    top = parts[0]
    if top == _RUNTIME_DATA_DIR:
        own = (_RUNTIME_DATA_DIR, "uploads", seg)
        depth = min(len(parts), len(own))
        if tuple(parts[:depth]) != own[:depth]:
            return False
        return len(parts) >= len(own) or is_dir
    scripts_parts = tuple(SCRIPTS_ROOT_REL.split("/"))
    if top == scripts_parts[0]:
        if tuple(parts[: len(scripts_parts)]) != scripts_parts[: len(parts)]:
            return False
        if len(parts) <= len(scripts_parts):
            return is_dir
        if len(parts) == len(scripts_parts) + 1 and not is_dir:
            return True  # scripts/tools/ 根下的共享脚本
        return parts[len(scripts_parts)] == seg
    if top == "skills":
        if len(parts) == 1:
            return is_dir
        return _skill_visible(parts[1])
    if top in _INTERNAL_TOP_DIRS:
        return False
    return True


def is_downloadable_by_grant(rel: str, *, uploads_rel: str = "data/uploads") -> bool:
    """下载授权（不含身份的路径级签名）能放行的路径。

    授权只在 send_file 发文件时由本服务签发，正常只会指向会话目录、共享脚本与已启用
    Skill 的资源。这里再收一道：持有签名密钥的一方（共享密钥也在宿主手里）即使伪造授权，
    也取不到记忆库、审计日志、LLM 原文、配置与源码。

    :param rel: 相对项目根的路径（须是已解析后的真实路径）
    :param uploads_rel: 上传根相对项目根的路径。调用方从 ``DEFAULT_UPLOADS_ROOT`` 推导，
        与上传落点、归属校验同源，挪一次目录这里不会静默失效
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p.startswith(".") for p in parts) or is_denied_name(parts[-1]):
        return False
    if _matches_escape_prefix("/".join(parts)):
        return True
    uploads_parts = [p for p in uploads_rel.replace("\\", "/").split("/") if p]
    if uploads_parts and parts[: len(uploads_parts)] == uploads_parts:
        return len(parts) >= len(uploads_parts) + 2  # {uploads}/{session}/...
    scripts_parts = SCRIPTS_ROOT_REL.split("/")
    if parts[: len(scripts_parts)] == scripts_parts:
        return len(parts) > len(scripts_parts)
    if parts[0] == "skills":
        return len(parts) >= 3 and _skill_visible(parts[1])
    return False
