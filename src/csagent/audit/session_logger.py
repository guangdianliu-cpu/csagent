"""会话审计日志持久化。

开启后写入 data/audit/sessions-YYYY-MM-DD.jsonl：每行一条摘要
（请求 / 回复 / 思考 / 工具调用 / 状态），是业务留痕，与 LOG_LEVEL 无关。

LLM 每轮的消息全文**不在这里**：原文由 data/llm_bodies/ 全量落盘（DEBUG 级别），
且比审计侧多出 tools schema、token 用量、请求响应分离与重试次数。摘要里只留
``llm_body_path`` 直链过去，不再另写一份有损副本。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

from csagent.audit.trace_buffer import AuditTraceBuffer

logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT_DIR = PROJECT_ROOT / "data" / "audit"


def resolve_audit_dir(audit_log_dir: str | None) -> Path:
    """``audit.dir`` / ``AUDIT_LOG_DIR`` → 绝对目录：空用默认，相对路径相对项目根。

    会话审计与安全审计（security_logger）共用，两份留痕必须落在同一处。
    """
    raw = str(audit_log_dir or "").strip()
    if not raw:
        return DEFAULT_AUDIT_DIR
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path

@lru_cache(maxsize=1)
def runtime_identity() -> tuple[str, str]:
    """``(部署形态, 运行实例)``：同一套代码可能在多台机器、多个形态下同时跑，
    审计要能分出是哪一个实例做的。实例 = 主机名:进程号。"""
    try:
        from csagent.infra.config import active_profile

        profile = active_profile()
    except Exception:  # noqa: BLE001
        profile = ""
    return profile, f"{socket.gethostname()}:{os.getpid()}"


def _local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class SessionAuditRecord:
    """单次会话对话审计记录。"""

    trace_id: str
    session_id: str
    channel: str
    user_message: str
    final_reply: str
    # interrupted：客户端断连，只落了已流出的半截回答（见 agent._finalize_interrupted_turn）
    status: str  # success | interrupted | stopped | escalation | blocked_input | blocked_output | error | skill_loaded
    domains: list[str] = field(default_factory=list)
    escalation_reason: str | None = None
    ticket_id: str | None = None
    violations: list[str] = field(default_factory=list)
    thought_trace: list[str] = field(default_factory=list)
    # 结构化思考事件（ThoughtEvent.to_dict()），可渲染时间线（M1.1）
    thought_events: list[dict[str, Any]] = field(default_factory=list)
    # 工具调用明细：耗时 / 参数摘要 / 结果摘要 / 状态（M2.2）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # LLM 轮次只留计数与原文直链；全文见 data/llm_bodies/
    llm_turn_count: int = 0
    llm_body_path: str | None = None
    # M4.1：API 回报的用量合计 + 各层本地占用。没有仪表就没法判断
    # M3.3 把窗口加大之后是变好了还是变贵了。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_layers: dict[str, Any] = field(default_factory=dict)
    # skill_loaded 事件附加字段
    skill_name: str | None = None
    skill_version: str | None = None
    skill_source: str | None = None
    # ---- 主体与环境：谁、经什么方式、在哪个实例、按哪版配置与策略 ----
    user_id: str = ""
    auth_method: str = ""
    chat_type: str = ""
    chat_id: str = ""
    profile: str = field(default_factory=lambda: runtime_identity()[0])
    instance_id: str = field(default_factory=lambda: runtime_identity()[1])
    # 本轮生效配置的指纹（系统提示词 + 工具集 + 护栏 / 策略 / 规则文件），规则改过之后
    # 仍能回答「当时是按哪版配置跑的」；policy_version 为 policy.yaml 的 版本@摘要
    config_digest: str = ""
    policy_version: str = ""
    # 状态改动证据（文件 sha256、脚本存档、发给了谁）与本轮发起的确认卡片
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    challenges: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: _local_now().isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionAuditLogger:
    """将审计记录追加写入 JSONL。"""

    def __init__(
        self,
        audit_dir: Path | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._audit_dir = audit_dir or DEFAULT_AUDIT_DIR
        if self._enabled:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
            self._queue: asyncio.Queue[SessionAuditRecord | None] = asyncio.Queue()
            self._writer_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        """启动后台写任务（在 lifespan 中调用）。"""
        if not self._enabled or self._writer_task is not None:
            return
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        """关闭队列并等待所有记录落盘（在 lifespan 退出时调用）。"""
        if not self._enabled or self._writer_task is None:
            return
        await self._queue.put(None)
        await self._writer_task
        self._writer_task = None

    async def _writer_loop(self) -> None:
        while True:
            record = await self._queue.get()
            if record is None:
                break
            try:
                await asyncio.to_thread(self._write_sync, record)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "audit_write_failed",
                    trace_id=record.trace_id,
                    error=str(exc),
                )

    def _write_sync(self, record: SessionAuditRecord) -> None:
        now = _local_now()
        day = now.strftime("%Y-%m-%d")
        path = self._audit_dir / f"sessions-{day}.jsonl"
        line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(
            "audit_record_written",
            trace_id=record.trace_id,
            status=record.status,
            path=str(path),
            llm_body_path=record.llm_body_path,
        )

    async def write(self, record: SessionAuditRecord) -> None:
        if not self._enabled:
            return
        await self._queue.put(record)

    def write_nowait(self, record: SessionAuditRecord) -> None:
        """同步入队，供**不能 await 的收尾路径**使用（如客户端断连后的中断落盘）。

        队列无容量上限，``put_nowait`` 不会阻塞也不会抛 QueueFull；真正的落盘仍由
        ``_writer_loop`` 在后台完成。异常一律吞掉并告警——收尾路径失败不该盖掉
        正在传播的取消信号。

        :param record: 待写入的审计记录
        """
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit_write_nowait_failed", trace_id=record.trace_id, error=str(exc))

    async def log_skill_loaded(
        self,
        *,
        session_id: str,
        channel: str,
        skill_name: str,
        skill_version: str = "",
        skill_source: str = "",
        trace_id: str = "",
    ) -> None:
        """记录 skill_loaded 审计事件。"""
        if not self._enabled:
            return
        import uuid as _uuid
        record = SessionAuditRecord(
            trace_id=trace_id or str(_uuid.uuid4()),
            session_id=session_id,
            channel=channel,
            user_message="",
            final_reply="",
            status="skill_loaded",
            skill_name=skill_name,
            skill_version=skill_version,
            skill_source=skill_source,
        )
        await self._queue.put(record)

    def fill_from_buffer(
        self,
        record: SessionAuditRecord,
        buffer: AuditTraceBuffer | None,
    ) -> SessionAuditRecord:
        """把请求缓冲里的思考 / 结构化事件 / 工具调用填进记录。

        这些都体积可控，始终记录；LLM 消息全文不进审计，只留计数、用量合计与原文直链。
        """
        if buffer is None:
            return record
        record.thought_trace = list(buffer.thoughts)
        record.thought_events = list(buffer.events)
        record.tool_calls = list(buffer.tool_calls)
        record.llm_turn_count = buffer.llm_turn_count
        record.llm_body_path = buffer.llm_body_path or None
        record.prompt_tokens = buffer.prompt_tokens
        record.completion_tokens = buffer.completion_tokens
        record.context_layers = dict(buffer.context_layers)
        record.config_digest = buffer.config_digest
        record.policy_version = buffer.policy_version
        record.state_changes = list(buffer.state_changes)
        record.challenges = list(buffer.challenges)
        return record
