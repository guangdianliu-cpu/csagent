"""数据集发布：管理后台上传 → 后台子进程校验并构建 → 原子生效。

通用层只做三件事：读 ``configs/datasets.yaml``、把上传落到暂存区、调 builder 脚本。
「文件属于哪张表、怎么校验、怎么建库」全在 builder 里（位于 skills/ 下），
这里不认识任何业务字段。

构建放在子进程里：大 Excel 解析吃 CPU 几十秒到数分钟，放在事件循环里会卡住所有对话。
同一数据集的发布串行执行，避免两个构建互相覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from csagent.audit.security_logger import record_security_event
from csagent.common.proc import kill_tree, spawn_kwargs
from csagent.infra.config import CONFIGS_DIR, PROJECT_ROOT
from csagent.tools.packs.script_pack import resolve_env_extra

logger = structlog.get_logger(__name__)

STAGING_DIR = PROJECT_ROOT / "data" / "admin-staging"
_DESCRIBE_TIMEOUT_SECONDS = 60
# 只保留最近这么多条任务记录（仅内存，供页面轮询）
_MAX_JOBS = 50


@dataclass(frozen=True)
class DatasetSpec:
    id: str
    title: str
    builder: Path
    description: str = ""
    accept: tuple[str, ...] = (".xlsx",)
    max_upload_bytes: int = 200 * 1024 * 1024
    publish_timeout_seconds: float = 1800.0
    env_extra: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "accept": list(self.accept),
            "max_upload_mb": self.max_upload_bytes // (1024 * 1024),
        }


@dataclass
class PublishJob:
    id: str
    dataset_id: str
    filename: str
    operator: str
    status: str = "queued"  # queued | running | succeeded | failed
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


def load_dataset_specs(path: Path | None = None) -> dict[str, DatasetSpec]:
    cfg_path = path or (CONFIGS_DIR / "datasets.yaml")
    if not cfg_path.is_file():
        return {}
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    specs: dict[str, DatasetSpec] = {}
    for item in raw.get("datasets") or []:
        ds_id = str(item.get("id") or "").strip()
        builder = str(item.get("builder") or "").strip()
        if not ds_id or not builder:
            continue
        specs[ds_id] = DatasetSpec(
            id=ds_id,
            title=str(item.get("title") or ds_id),
            description=str(item.get("description") or ""),
            builder=(PROJECT_ROOT / builder).resolve(),
            accept=tuple(str(x).lower() for x in (item.get("accept") or [".xlsx"])),
            max_upload_bytes=int(item.get("max_upload_mb") or 200) * 1024 * 1024,
            publish_timeout_seconds=float(item.get("publish_timeout_seconds") or 1800),
            env_extra=tuple(str(x) for x in (item.get("env_extra") or [])),
        )
    return specs


async def _run_builder(
    spec: DatasetSpec, args: list[str], timeout: float
) -> tuple[int, dict[str, Any] | None, str]:
    """跑 builder，返回 (退出码, stdout 末行 JSON, stderr 尾部)。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(spec.builder), *args,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **spawn_kwargs(),
        env={
            **os.environ,
            # .env 里的值不会进 os.environ，按名单补给子进程（与 run_script 同一机制）
            **resolve_env_extra(list(spec.env_extra)),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        return -1, None, f"超时（{int(timeout)} 秒）"
    except asyncio.CancelledError:
        await kill_tree(proc)
        raise
    payload: dict[str, Any] | None = None
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    if lines:
        try:
            parsed = json.loads(lines[-1])
            payload = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            payload = None
    return proc.returncode or 0, payload, err.decode("utf-8", "replace")[-2000:]


class DatasetPublisher:
    """持有数据集配置、发布锁与任务表。进程内单例（挂在 app.state）。"""

    def __init__(self, specs: dict[str, DatasetSpec] | None = None) -> None:
        self._specs = specs if specs is not None else load_dataset_specs()
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, PublishJob] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def specs(self) -> dict[str, DatasetSpec]:
        return self._specs

    def get(self, dataset_id: str) -> DatasetSpec | None:
        return self._specs.get(dataset_id)

    async def describe(self, spec: DatasetSpec) -> dict[str, Any]:
        code, payload, err = await _run_builder(
            spec, ["--describe"], _DESCRIBE_TIMEOUT_SECONDS
        )
        if code != 0 or payload is None:
            logger.error("dataset_describe_failed", dataset=spec.id, code=code, stderr=err)
            return {"error": f"读取数据状态失败：{err.strip()[-300:] or code}"}
        return payload

    def job(self, job_id: str) -> PublishJob | None:
        return self._jobs.get(job_id)

    def jobs_for(self, dataset_id: str) -> list[PublishJob]:
        return sorted(
            (j for j in self._jobs.values() if j.dataset_id == dataset_id),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def submit(
        self, spec: DatasetSpec, staged: Path, filename: str, operator: str
    ) -> PublishJob:
        """登记一条发布任务并在后台执行；立即返回任务（页面据 id 轮询）。"""
        job = PublishJob(
            id=uuid.uuid4().hex[:12], dataset_id=spec.id,
            filename=filename, operator=operator,
        )
        self._jobs[job.id] = job
        self._trim_jobs()
        task = asyncio.create_task(self._publish(spec, job, staged))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _publish(self, spec: DatasetSpec, job: PublishJob, staged: Path) -> None:
        lock = self._locks.setdefault(spec.id, asyncio.Lock())
        try:
            async with lock:
                job.status = "running"
                logger.info(
                    "dataset_publish_start",
                    dataset=spec.id, job=job.id, file=job.filename, operator=job.operator,
                )
                code, payload, err = await _run_builder(
                    spec,
                    ["--publish", str(staged), "--filename", job.filename, "--json"],
                    spec.publish_timeout_seconds,
                )
                if code == 0 and payload and payload.get("ok"):
                    job.status = "succeeded"
                    job.result = payload
                    logger.info(
                        "dataset_publish_succeeded",
                        dataset=spec.id, job=job.id, file=job.filename, operator=job.operator,
                    )
                else:
                    job.status = "failed"
                    job.error = (
                        (payload or {}).get("error")
                        or err.strip()[-500:]
                        or f"构建进程异常退出（{code}）"
                    )
                    logger.error(
                        "dataset_publish_failed",
                        dataset=spec.id, job=job.id, file=job.filename,
                        operator=job.operator, error=job.error,
                    )
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"发布异常：{exc}"
            logger.exception("dataset_publish_crashed", dataset=spec.id, job=job.id)
        finally:
            job.finished_at = time.time()
            staged.unlink(missing_ok=True)
            # 发布直接覆盖线上数据，结果必须留痕（上传时已记过文件 sha256，按 job_id 对得上）
            record_security_event(
                "dataset_publish_finished",
                outcome="success" if job.status == "succeeded" else "failure",
                actor=job.operator,
                dataset_id=spec.id,
                job_id=job.id,
                file=job.filename,
                error=job.error or "",
            )

    def _trim_jobs(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        done = sorted(
            (j for j in self._jobs.values() if j.status in ("succeeded", "failed")),
            key=lambda j: j.created_at,
        )
        for j in done[: len(self._jobs) - _MAX_JOBS]:
            self._jobs.pop(j.id, None)
