"""停止 / 取消时脚本子进程（含孙进程）必须被清理，不能留下孤儿。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from csagent.tools.packs.script_pack import _run_subprocess

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="用 os.kill(pid, 0) 探活")

_SCRIPT = """
import subprocess, sys, time, pathlib
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(f"{child.pid}")
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 僵尸进程也算已结束
    try:
        with open(f"/proc/{pid}/status") as fh:
            return "State:\tZ" not in fh.read()
    except OSError:
        return True


async def _start(tmp_path: Path, timeout: int):
    script = tmp_path / "s.py"
    script.write_text(_SCRIPT)
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(_run_subprocess(
        [sys.executable, str(script), str(pidfile)],
        cwd=tmp_path, env=dict(os.environ), timeout_seconds=timeout,
        max_output_chars=1000, script_label="t",
    ))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    return task, int(pidfile.read_text())


async def test_cancel_kills_script_and_grandchild(tmp_path: Path):
    task, grandchild = await _start(tmp_path, timeout=60)
    assert _alive(grandchild)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(grandchild), "取消后孙进程仍在运行"


async def test_timeout_kills_grandchild(tmp_path: Path):
    task, grandchild = await _start(tmp_path, timeout=1)
    result = await task
    assert "超时" in result
    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(grandchild)
