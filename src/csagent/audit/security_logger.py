"""安全事件审计：谁动了权限边界。

写入 ``data/audit/security-YYYY-MM-DD.jsonl``，一行一个事件：管理员登录、签发调试令牌、
数据集发布、悬浮图标变更、Skill 重载、凭下载授权取文件等。

与会话审计（``sessions-*.jsonl``）分开存：那份按对话轮次记「谁问了什么、答了什么」，
这份记「谁做了越过普通用户权限的事」。两者都是业务留痕，跟随 ``audit.enabled``，
**不跟随 LOG_LEVEL**——生产 ``LOG_LEVEL=ERROR`` 时 ``logger.info / warning`` 一律不落盘，
以前管理员登录、签调试令牌这些事件在生产里直接丢了。

写入是同步追加：事件量很小（一次请求最多一两条），不值得为它维护队列与生命周期。
任何写入失败只告警不抛——审计写不进去不能反过来让管理操作或下载失败。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from csagent.audit.session_logger import resolve_audit_dir

logger = structlog.get_logger(__name__)

# 单个字段的字符上限：事件里不该出现大段正文，超长多半是误传
_MAX_FIELD_CHARS = 500


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + "…"
    if isinstance(value, list):
        return [_clip(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in value.items()}
    return value


def _client_fields(request: Any) -> dict[str, str]:
    """从请求里取来源信息。服务在 nginx 后面，X-Forwarded-For 原样记一份供排查。"""
    if request is None:
        return {}
    fields: dict[str, str] = {}
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client is not None else ""
    if host:
        fields["client_ip"] = str(host)
    headers = getattr(request, "headers", None)
    if headers is not None:
        forwarded = str(headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            fields["forwarded_for"] = forwarded[:200]
        agent = str(headers.get("user-agent") or "").strip()
        if agent:
            fields["user_agent"] = agent[:200]
    return fields


class SecurityAuditLogger:
    """把安全事件追加写入按天切分的 JSONL。"""

    def __init__(self, audit_dir: Path, *, enabled: bool = True) -> None:
        self._audit_dir = audit_dir
        self._enabled = enabled
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def audit_dir(self) -> Path:
        return self._audit_dir

    def record(
        self,
        event: str,
        *,
        outcome: str = "success",
        actor: str = "",
        request: Any = None,
        **fields: Any,
    ) -> None:
        """记一条安全事件。

        :param event: 事件名，如 ``admin_login`` / ``admin_debug_token_issued``
        :param outcome: ``success`` / ``failure`` / ``denied`` 等
        :param actor: 发起人（管理员用户名、会话 uid）；未知留空
        :param request: FastAPI Request，用来记来源 IP 与 UA；可不传
        :param fields: 事件专属字段。**不要传令牌、口令或密钥本身**
        """
        if not self._enabled:
            return
        now = datetime.now().astimezone()
        record: dict[str, Any] = {
            "ts": now.isoformat(),
            "event": event,
            "outcome": outcome,
            "actor": actor,
            **_client_fields(request),
            **{key: _clip(value) for key, value in fields.items()},
        }
        path = self._audit_dir / f"security-{now.strftime('%Y-%m-%d')}.jsonl"
        try:
            with self._lock:
                self._audit_dir.mkdir(parents=True, exist_ok=True)
                line = json.dumps(record, ensure_ascii=False, default=str)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            logger.error("security_audit_write_failed", audit_event=event, error=str(exc))


_instance: SecurityAuditLogger | None = None
_instance_lock = threading.Lock()


def configure_security_audit(audit_dir: Path, *, enabled: bool = True) -> SecurityAuditLogger:
    """显式指定落盘目录与开关（main.py 装配、测试隔离时用）。"""
    global _instance
    with _instance_lock:
        _instance = SecurityAuditLogger(audit_dir, enabled=enabled)
        return _instance


def security_audit() -> SecurityAuditLogger:
    """当前生效的安全审计实例；未显式配置时按 Settings 的审计目录与开关懒创建。"""
    global _instance
    if _instance is None:
        from csagent.infra.config import get_settings

        settings = get_settings()
        with _instance_lock:
            if _instance is None:
                _instance = SecurityAuditLogger(
                    resolve_audit_dir(settings.audit_log_dir),
                    enabled=bool(settings.audit_log_enabled),
                )
    return _instance


def record_security_event(event: str, **kwargs: Any) -> None:
    """``security_audit().record(...)`` 的简写，供路由处一行调用。"""
    security_audit().record(event, **kwargs)
