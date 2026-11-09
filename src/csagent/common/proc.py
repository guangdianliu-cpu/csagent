"""子进程生命周期：启动时自成进程组，结束时连同子孙一起杀掉。

为什么需要
----------
工具（run_script / run_shell / rg / 浏览器出 PDF）都在 asyncio 子进程里跑。以前只在
``TimeoutError`` 与 ``Exception`` 分支里 kill，而用户点「停止」或外层兜底超时取消任务时，
抛进来的是 ``CancelledError``（``BaseException``，不是 ``Exception``），子进程就被丢下
继续跑，成了孤儿；脚本自己再拉起的子进程更是从来没人管。

用法::

    proc = await asyncio.create_subprocess_exec(*cmd, ..., **spawn_kwargs())
    try:
        out = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        await kill_tree(proc)
        raise
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")


def spawn_kwargs() -> dict[str, Any]:
    """传给 ``create_subprocess_exec`` 的参数：让子进程自成一个进程组，便于整组清理。"""
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    return {"start_new_session": True}


async def kill_tree(process: asyncio.subprocess.Process | None, *, wait: float = 5.0) -> None:
    """杀掉进程及其所有子孙；已退出则什么都不做。永不抛异常。"""
    if process is None or process.returncode is not None:
        return
    try:
        if _IS_WINDOWS:
            # /T 连同子进程树，/F 强制
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=wait, check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except PermissionError:
                # 没能自成进程组（被外部 spawn 的进程）：退回只杀自己
                process.kill()
    except Exception as exc:  # noqa: BLE001
        logger.warning("kill_tree_failed", pid=process.pid, error=str(exc))
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=wait)
    except BaseException:  # noqa: BLE001 - 收尾阶段不能再把取消/超时抛出去
        pass
