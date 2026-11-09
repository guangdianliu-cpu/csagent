"""在子进程内还原 sys.argv，再以 __main__ 执行目标脚本。

run_script 若把 --payload 这类带引号的 JSON 直接塞进 argv，Windows CreateProcess
会先拼成一条命令行；PyCharm pydevd 再 parse 一遍时引号就被吃掉
（见 audit wecom_BoWen：payload 不是合法 JSON / char 1）。

真正的参数放在一份 job JSON 里，命令行只传 runner + job 路径。目标脚本仍在
**同一个** Python 进程里 runpy，Debug 子进程时 query.py 上的断点仍然有效。
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("script_argv_runner: expected a single job file path")
    job_path = Path(args[0])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    script = str(job["script"])
    script_args = [str(a) for a in (job.get("argv") or [])]
    sys.argv = [script, *script_args]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
