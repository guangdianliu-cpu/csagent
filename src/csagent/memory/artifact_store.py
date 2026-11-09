"""产物登记：会话收发文件的持久清单（M1c / M2.4）。

**登记不是排队**，这两件事容易混为一谈：

- 排队（``channels/outbound_files.py``）：``send_file`` 入队、通道在本轮文字回复
  结束后取走。生命周期是**单轮内、同进程**，毫秒级，进程内 dict 就是对的实现。
- 登记（本模块）：把「这个会话产出/收到过哪些文件」永久记下来。它要回答的是
  一个月后的「上次那个报表还在吗」，以及 M3.4 的跨 episode 召回要搜的文件名。

两者的写入点重合（都在文件产生的那一刻），但存活期差了七个数量级，所以分开实现。

**登记必须是尽力而为**：任何异常都只告警不外抛。文件已经写好了、也已经入队了，
不能因为一条清单没记上就让上传或发送失败。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import structlog

from csagent.memory.sqlite_schema import connect, ensure_schema

logger = structlog.get_logger(__name__)

# 超过这个大小就不算 sha256：清单的价值在「记下来过」，不值得为校验和
# 在上传/发送的热路径上读一遍几百 MB。超限时 sha256 留空。
_HASH_SIZE_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactRecord:
    """一条产物登记。"""

    id: str
    session_id: str
    kind: str  # inbound / outbound / script / tool_result
    path: str  # 工作区相对路径
    filename: str
    size: int
    created_at: float
    sha256: str = ""

    @property
    def exists(self) -> bool:
        """文件当前是否还在盘上。

        登记永久保留，文件却可能被工作区清理删掉（或被用户手工删除），
        两者会分叉。回答「还在吗」必须真的去看一眼，不能只信清单。
        """
        return Path(self.path).exists()


class ArtifactStore:
    """产物清单，与记忆共用 ``data/csagent.db``。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        ensure_schema(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def register(
        self,
        *,
        session_id: str,
        kind: str,
        path: str,
        filename: str,
        size: int = 0,
        episode_id: str = "",
        turn_id: int | None = None,
        expires_at: float | None = None,
        compute_hash: bool = True,
        artifact_id: str = "",
    ) -> str:
        """登记一个产物，返回 artifact id；失败返回空串。

        :param session_id: 所属会话
        :param kind: ``inbound`` 用户上传 / ``outbound`` 发给用户 /
                     ``script`` 会话脚本 / ``tool_result`` 外置的大工具结果
        :param path: 工作区相对路径（与 ``messages.attachments`` 里存的同一口径）
        :param filename: 文件名，供事后按名检索
        :param size: 字节数；传 0 时尝试从磁盘读取
        :param episode_id: 所属 episode，留空表示暂不关联
        :param turn_id: 所属轮次
        :param expires_at: 过期时间戳；``None`` 表示永不过期（默认）
        :param compute_hash: 是否计算 sha256（超过 16MB 一律跳过）
        :param artifact_id: 由调用方指定的 id。留空则本方法生成。
            外置大结果（``artifact_offload``）需要**先知道 id 才能命名文件**，
            否则就得先插一条空路径再回填，把裸 SQL 漏到调用方去。
        :returns: artifact id；登记失败返回空串（调用方不应依赖它）
        """
        if not session_id or not path:
            return ""

        artifact_id = artifact_id or uuid.uuid4().hex
        try:
            resolved = Path(path)
            if size <= 0 and resolved.is_file():
                size = resolved.stat().st_size
            digest = self._safe_hash(resolved) if compute_hash else ""

            with closing(connect(self._path)) as conn, conn:
                conn.execute(
                    "INSERT INTO artifacts"
                    " (id, session_id, episode_id, turn_id, kind, path, filename,"
                    "  sha256, size, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id, session_id, episode_id, turn_id, kind,
                        path, filename, digest, size, time.time(), expires_at,
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            # 文件已经写好也已经入队了，清单没记上不该让上传/发送失败
            logger.warning(
                "artifact_register_failed",
                session_id=session_id, kind=kind, path=path, error=str(exc),
            )
            return ""

        logger.debug(
            "artifact_registered",
            session_id=session_id, kind=kind, filename=filename, size=size,
        )
        return artifact_id

    @staticmethod
    def _safe_hash(path: Path) -> str:
        """算 sha256；文件不存在、过大或读不动时返回空串。"""
        try:
            if not path.is_file() or path.stat().st_size > _HASH_SIZE_LIMIT:
                return ""
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        """按 id 取一条登记；不存在或查询出错时返回 None。

        **调用方必须自己校验 ``session_id`` 归属。** id 是 uuid4，猜不出来，但
        「猜不出来」不是访问控制——外置结果里可能有另一个用户的明细，
        这条边界要由调用点显式守住，不能靠 id 的熵。

        :param artifact_id: 登记 id
        """
        if not artifact_id:
            return None
        try:
            with closing(connect(self._path)) as conn:
                row = conn.execute(
                    "SELECT id, session_id, kind, path, filename, size, created_at, sha256"
                    " FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("artifact_get_failed", artifact_id=artifact_id, error=str(exc))
            return None
        return ArtifactRecord(*row) if row else None

    def find(
        self,
        *,
        session_id: str = "",
        filename_like: str = "",
        kind: str = "",
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        """按会话 / 文件名片段 / 类别检索，按时间倒序（最近的在前）。

        ``filename_like`` 做的是子串匹配，服务于「上次那个叫什么报表的文件」
        这类只记得半个名字的查询。

        :returns: 匹配的登记；查询出错时返回空列表
        """
        clauses: list[str] = []
        params: list[object] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if filename_like:
            clauses.append("filename LIKE ?")
            params.append(f"%{filename_like}%")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        try:
            with closing(connect(self._path)) as conn:
                rows = conn.execute(
                    "SELECT id, session_id, kind, path, filename, size, created_at, sha256"
                    f" FROM artifacts{where} ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("artifact_find_failed", error=str(exc))
            return []
        return [ArtifactRecord(*row) for row in rows]


# ========== 进程级接入点 ==========
#
# 登记点分散在工具包与 HTTP 路由里，它们都拿不到 app.state（工具包由 ToolPackLoader
# 按 YAML 构造，不经过 FastAPI）。沿用 outbound_files / todo_pack 已有的模块级单例
# 做法：启动时注入一次，用不到的场景（测试、非 sqlite 后端）保持 None 静默跳过。

_store: ArtifactStore | None = None


def set_artifact_store(store: ArtifactStore | None) -> None:
    """启动时注入；传 None 表示关闭登记（非 sqlite 后端 / 测试）。"""
    global _store
    _store = store


def get_artifact_store() -> ArtifactStore | None:
    return _store


def register_artifact(**kwargs) -> str:
    """便捷入口：store 未注入时静默跳过，调用方无需判空。

    **这里吞掉所有异常**，而不只是 store 内部预期的 sqlite/OS 错误。
    「登记失败不连累上传与发送」这条保证必须落在调用边界上——调用点位于
    HTTP 上传与 send_file 的成功路径末尾，文件那时已经落盘、已经入队，
    任何从这里抛出去的东西都会把一次成功的操作变成 500。

    参数同 ``ArtifactStore.register``。

    :returns: artifact id；未注入或登记失败时返回空串
    """
    if _store is None:
        return ""
    try:
        return _store.register(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact_register_failed", error=str(exc))
        return ""
