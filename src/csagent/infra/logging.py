"""日志体系：一个 LOG_LEVEL 主开关 + 控制台 / error.log / 可选 Graylog。

结构对齐 logback：
- **root level** = ``LOG_LEVEL``（ERROR 生产 / INFO 测试 / DEBUG 排查）
- **appender** = 控制台（stdout，跟随 ``LOG_LEVEL``）
  + ``data/logs/error.log``（**恒定 ERROR，不跟随 LOG_LEVEL**）
  旧的全量镜像 ``app.log`` 已删除：DEBUG 下它几乎全是心跳噪音，而排查要的全文
  在 ``data/llm_bodies/``、复核要的留痕在 ``data/audit/``，那份副本没有读者。
  留下的 ``error.log`` 只收 ERROR，因此在任何 LOG_LEVEL 下都很小，却是**唯一
  长期留存的故障轨迹**——渠道掉线、启动失败、LLM 限流这些都不产生 audit 记录。
  想让某件事出现在这里，就得用 ``logger.error`` 打；见 llm_client 的
  ``llm_rate_limited``（429）与各处放弃重试的分支。
  ``GRAYLOG_ENABLED`` 打开后，同一批 ERROR 再走 GELF TCP 推到 Graylog。
- structlog 只负责把结构化字段渲染好，落地统一交给标准库 logging，
  这样第三方库（uvicorn / httpx / redis）的日志也走同一套 handler 与格式。

另有一类**落盘产物**跟随同一个 LOG_LEVEL，见 config._resolve_log_sinks：
- ``data/llm_bodies/`` LLM 原始请求/响应全文（DEBUG）
会话审计 ``data/audit/sessions-*.jsonl`` 是业务留痕，不随级别关闭；其中
``llm_body_path`` 直链到上面的原文文件。

通过 contextvars 自动为每条日志附加 trace_id / session_id / channel /
agent_name / domain / task_id，保证跨层日志可按同一 trace 串起来。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import structlog

_CONFIGURED = False

# 第三方库默认过于聒噪，统一压到 WARNING；LOG_LEVEL=DEBUG 时再放开。
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "asyncio",
    "websockets",
    "redis",
    "uvicorn.access",
)


def _ensure_utf8_stdio() -> None:
    """Windows 默认 GBK 控制台遇到 emoji 会 UnicodeEncodeError 并打断业务。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _parse_level(log_level: str) -> int:
    return getattr(logging, (log_level or "INFO").strip().upper(), logging.INFO)


def _shared_processors() -> list[Any]:
    """structlog 与标准库两条链都要走的处理器。"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def _make_formatter(log_format: str) -> logging.Formatter:
    """LOG_FORMAT=console 走彩色可读渲染，=json 走 JSON（便于采集侧 grep）。"""
    if log_format == "json":
        # 默认 ensure_ascii 会把中文写成 \uXXXX。Graylog 告警原文照抄这行，
        # 渠道再吃掉反斜杠，文件名就变成 u3010u5229…。直接写 UTF-8。
        renderer: Any = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    return structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=_shared_processors(),
    )


_DEFAULT_SERVICE = Path(__file__).resolve().parents[3].name or "service"


class _GelfTcpHandler(logging.Handler):
    """把 ERROR 行异步送到 Graylog（GELF TCP，每条 JSON 以 \\0 结尾）。

    连不上或队列满就丢，不阻塞业务线程，也不把发送失败再打回 root（会递归）。
    """

    def __init__(self, host: str, port: int, facility: str) -> None:
        super().__init__(level=logging.ERROR)
        self._host = host
        self._port = port
        self._service = _DEFAULT_SERVICE
        self._facility = facility or self._service
        self._queue: queue.Queue[tuple[str, str, str, str]] = queue.Queue(maxsize=500)
        self._worker = threading.Thread(
            target=self._drain, name="graylog-gelf", daemon=True
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            self._queue.put_nowait((text, record.levelname.upper(), record.name, record.threadName))
        except queue.Full:
            return
        except Exception:
            return

    def _drain(self) -> None:
        while True:
            text, severity, logger_name, thread_name = self._queue.get()
            trace_id = None
            if text.startswith("{"):
                try:
                    meta = json.loads(text)
                    if isinstance(meta, dict):
                        trace_id = meta.get("trace_id") or meta.get("traceId")
                except Exception:
                    pass

            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            payload: dict[str, Any] = {
                "version": "1.1",
                "host": socket.gethostname(),
                "short_message": text[:1500],
                "full_message": text[:8000],
                "timestamp": time.time(),
                "level": 3,
                "facility": self._facility,
                "_service": self._service,
                "_Severity": severity or "ERROR",
                "_LoggerName": logger_name or "root",
                "_Thread": thread_name or "main",
                "_Time": now_str,
            }
            if trace_id:
                payload["_traceId"] = str(trace_id)

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\0"
            try:
                with socket.create_connection((self._host, self._port), timeout=2) as sock:
                    sock.sendall(data)
            except OSError:
                continue


def configure_logging(
    log_level: str = "INFO",
    log_format: str = "console",
    *,
    error_log_enabled: bool = False,
    error_log_dir: str = "",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    project_root: Path | None = None,
    graylog_enabled: bool = False,
    graylog_host: str = "",
    graylog_port: int = 12201,
    graylog_facility: str = "ld308_csagent",
) -> None:
    """配置全局日志。重复调用是幂等的（先清空 root handlers）。

    ``error_log_*`` 控制的 ERROR 文件 appender 与 ``log_level`` 相互独立：
    生产跑 ERROR、排查跑 DEBUG，落到 error.log 的内容都只有 ERROR。
    """
    global _CONFIGURED
    _ensure_utf8_stdio()
    level = _parse_level(log_level)

    structlog.configure(
        processors=[
            *_shared_processors(),
            # 交给 ProcessorFormatter 渲染，避免 structlog 与 stdlib 各渲染一次
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_make_formatter(log_format))
    root.addHandler(console)

    if error_log_enabled and error_log_dir:
        directory = Path(error_log_dir)
        if not directory.is_absolute():
            base = project_root or Path(__file__).resolve().parents[3]
            directory = base / directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            error_file = logging.handlers.RotatingFileHandler(
                directory / "error.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            # 恒定 ERROR：root level 放到 DEBUG 也不会把流水冲进这个文件
            error_file.setLevel(logging.ERROR)
            # 文件恒用 JSON：控制台是给人看的，文件是给 grep / 采集用的
            error_file.setFormatter(_make_formatter("json"))
            root.addHandler(error_file)
        except OSError as exc:  # pragma: no cover - 磁盘不可写时不该拖垮启动
            root.warning("error_log_handler_init_failed: %s", exc)

    # ---------- Graylog：与 error.log 同级，只收 ERROR ----------
    if graylog_enabled and graylog_host:
        gelf = _GelfTcpHandler(graylog_host, int(graylog_port), graylog_facility)
        gelf.setFormatter(_make_formatter("json"))
        root.addHandler(gelf)

    noisy_level = level if level <= logging.DEBUG else max(level, logging.WARNING)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    _CONFIGURED = True


def is_configured() -> bool:
    return _CONFIGURED


def bind_context(**kwargs: Any) -> dict[str, Any]:
    """绑定若干上下文变量，返回 token 字典供 reset_context 还原。

    用 token 而非 clear_contextvars，避免误清除外层已绑定的 trace_id 等。
    """
    return dict(structlog.contextvars.bind_contextvars(**kwargs))


def reset_context(tokens: dict[str, Any]) -> None:
    """按 token 还原上下文变量，恢复到绑定前的值。"""
    structlog.contextvars.reset_contextvars(**tokens)
