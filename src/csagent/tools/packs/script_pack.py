"""脚本工具包：写脚本 / 跑脚本 / 执行 shell（内部能力，限制从简）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from csagent.common.proc import kill_tree, spawn_kwargs
# 继承模式下从子进程环境里剥掉名字像密钥的变量；脚本真需要的凭证在 tool_packs.yaml
# 的 env_extra 里按名声明，只注入给 Skill 自带脚本
from csagent.common.secrets import is_secret_env_name

from csagent.channels.workspace_uploads import (
    bind_session_script_path,
    safe_session_segment,
    session_inbound_rel,
    session_outbound_rel,
    session_scripts_rel,
    session_uploads_rel,
)
from csagent.infra.config import PROJECT_ROOT
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolCallError, ToolPack
from csagent.tools.skill_paths import (
    is_skill_path,
    resolve_skill_resource,
    skill_python_paths,
    skill_roots,
)

logger = structlog.get_logger(__name__)

# tools/script_argv_runner.py：.py 的 argv 走 job 文件，避免 JSON 进 Windows 命令行
_ARGV_RUNNER = Path(__file__).resolve().parents[1] / "script_argv_runner.py"

# 与 react.max_tool_result_chars 对齐。工具里先切到 8000 的话，3 万外置层永远
# 看不到超限结果，Skill / 查询脚本的大 stdout 会在进 ReAct 之前丢掉尾巴。
# 超过本值仍硬切并标注；3 万以上由 handlers._shrink_tool_result 外置。
DEFAULT_MAX_OUTPUT_CHARS = 30000

# skill_scripts_only 部署下，拒绝非 Skill 脚本时回给模型的说明
_SKILL_SCRIPTS_ONLY_HINT = (
    "当前部署只允许运行 Skill 自带脚本：script=\"<skill:name>/scripts/x.py\"，"
    "确切路径与参数见对应 Skill 的 load_skill 结果；不能运行会话脚本或共享脚本。"
)


def _reject_dotdot(rel: str) -> str | None:
    if any(part == ".." for part in Path(rel.replace("\\", "/")).parts):
        return "错误: 路径不得包含 .."
    return None


def resolve_env_extra(
    names: list[str] | None,
    *,
    project_root: Path | None = None,
) -> dict[str, str]:
    """把「要额外透传给脚本子进程的变量名」解析成 name→value。

    为什么需要它：``.env`` 是 pydantic Settings 的 env_file，值只进 Settings 对象，
    **不会回写 os.environ**；而 Skill 脚本由 run_script 拉起子进程、读的是 os.environ。
    于是「在 .env 里配一个变量」对脚本是无效的（见 audit 2026-08-16 产品中心网关）。

    约定：**变量名写在 tool_packs*.yaml（按产品形态不同），值留在 .env（单一配置面）**。
    通用源码因此不需要认识任何具体变量名。

    取值优先级：os.environ > .env 文件。两处都没有的名字直接跳过，不注入空串——
    否则会盖掉脚本自身的默认值。
    """
    if not names:
        return {}
    root = (project_root or PROJECT_ROOT).resolve()
    from_file: dict[str, str | None] = {}
    try:
        from dotenv import dotenv_values

        from csagent.infra.config import env_files

        # .env 在前、.env.<profile> 在后覆盖，与 Settings 的口径一致
        for env_path in env_files():
            env_path = root / env_path.name
            if env_path.is_file():
                from_file.update(dotenv_values(env_path, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 读不到 .env 不该拖垮工具装配
        logger.warning("env_extra_dotenv_read_failed", error=str(exc))

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        value = os.environ.get(name)
        if value is None:
            value = from_file.get(name)
        if value is None:
            missing.append(name)
            continue
        resolved[name] = str(value)
    if missing:
        logger.info("env_extra_unset", names=missing, hint="脚本将使用其自身默认值")
    return resolved


def _build_scope_env(
    scope: CustomerScope,
    *,
    sandbox: bool = False,
    env_passthrough: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
    project_root: Path | None = None,
) -> dict[str, str]:
    """构造子进程环境变量。

    env_passthrough=None（默认）：继承 os.environ，但剥掉名字像密钥的变量
    （见 ``is_secret_env_name``）。PATH、PYTHONPATH、代理、区域设置这类运行所需的
    变量照常继承——白名单模式容易漏掉它们把线上脚本跑挂，所以默认走剥离。
    env_passthrough=[...]：白名单模式，只继承列出的变量名（大小写不敏感），
    显式列出的名字即使像密钥也照给。

    另注入「工作区契约」变量（见 _WORKSPACE_ENV_DOC）：脚本靠它定位项目根、
    会话目录与 Skill 根，不必猜相对路径、更不该把部署绝对路径写死进脚本。
    这些是本进程主动注入的约定变量，不受白名单过滤。
    """
    if env_passthrough is None:
        env = {
            k: v
            for k, v in os.environ.items()
            if isinstance(v, str) and not is_secret_env_name(k)
        }
    else:
        allow = {name.upper() for name in env_passthrough}
        env = {
            k: v
            for k, v in os.environ.items()
            if isinstance(v, str) and k.upper() in allow
        }
    if sandbox:
        for key in ("PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
            env.pop(key, None)
    env["CSAGENT_SESSION_ID"] = scope.session_id
    env["CSAGENT_CHANNEL"] = scope.channel
    # 策略与审计用的主体：脚本日志能对上「谁、哪一轮、哪个实例」，不靠模型转述
    from csagent.audit.session_logger import runtime_identity

    profile, instance_id = runtime_identity()
    env["CSAGENT_TRACE_ID"] = scope.trace_id
    env["CSAGENT_USER_ID"] = scope.user_id
    env["CSAGENT_AUTH_METHOD"] = scope.auth_method
    env["CSAGENT_CHAT_TYPE"] = scope.chat_type
    env["CSAGENT_CHAT_ID"] = scope.chat_id
    env["CSAGENT_PROFILE"] = profile
    env["CSAGENT_INSTANCE_ID"] = instance_id

    root = (project_root or PROJECT_ROOT).resolve()
    env["CSAGENT_PROJECT_ROOT"] = str(root)
    env["CSAGENT_SESSION_DIR"] = str(root / session_uploads_rel(scope.session_id))
    env["CSAGENT_INBOUND_DIR"] = str(root / session_inbound_rel(scope.session_id))
    env["CSAGENT_OUTBOUND_DIR"] = str(root / session_outbound_rel(scope.session_id))
    env["CSAGENT_SCRIPT_DIR"] = str(root / session_scripts_rel(scope.session_id))
    roots = skill_roots()
    if roots:
        env["CSAGENT_SKILLS_ROOTS"] = os.pathsep.join(str(p) for p in roots)

    # 配置声明要透传的变量（如产品中心网关地址/token）；在白名单模式下也必须生效，
    # 否则声明了却传不进去
    if env_extra:
        env.update(env_extra)

    # Windows 子进程 stdout 默认走活动代码页（常见 GBK），不强制会和下面的
    # utf-8 decode 对不上产生乱码；这里无条件设置，不受 env_passthrough 白名单影响
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# 失败时 stderr 只留结尾这么多：Python 的异常在 traceback 最后一行
_STDERR_TAIL_CHARS = 2000

# 子进程里包了一层 script_argv_runner + runpy，这几层栈帧对模型没有用，只会把真正的异常挤到后面
_RUNNER_FRAME_RE = re.compile(r'^  File "(?:[^"]*script_argv_runner\.py|<frozen runpy>|[^"]*[\\/]runpy\.py)"')


def _clean_traceback(stderr: str) -> str:
    """去掉 traceback 里 runner / runpy 的栈帧（连同它们的源码行与 ^^^ 标记行）。"""
    out: list[str] = []
    skipping = False
    for line in stderr.splitlines():
        if line.startswith("  File "):
            skipping = bool(_RUNNER_FRAME_RE.match(line))
            if skipping:
                continue
        elif skipping and line.startswith("    "):
            continue
        else:
            skipping = False
        out.append(line)
    return "\n".join(out)


def _exception_line(stderr: str) -> str:
    """traceback 的最后一行（如 ``KeyError: 'nav'``）；没有 traceback 时返回空串。"""
    if "Traceback (most recent call last)" not in stderr:
        return ""
    for line in reversed(stderr.splitlines()):
        if line.strip() and not line.startswith(" "):
            return line.strip()[:300]
    return ""


def _prepend_pythonpath(env: dict[str, str], paths: list[Path]) -> None:
    """把 Skill 声明的模块目录排到 PYTHONPATH 最前面：会话脚本直接 import，不用自己拼 sys.path。

    沙箱模式先剥掉了继承来的 PYTHONPATH（防注入），这里加的是 Skill 目录，不受影响。
    """
    if not paths:
        return
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*(str(p) for p in paths), *([current] if current else [])])


def _write_argv_job(script_path: Path, script_args: list[str]) -> Path:
    """把脚本路径和 argv 写到临时 job 文件，供 script_argv_runner 还原 sys.argv。"""
    fd, raw = tempfile.mkstemp(prefix="csagent_argv_", suffix=".json")
    path = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "script": str(script_path),
                    "argv": [str(a) for a in script_args],
                },
                fh,
                ensure_ascii=False,
            )
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _kill_process(process: asyncio.subprocess.Process | None) -> None:
    """连同脚本拉起的子孙进程一起清理（见 common/proc.py）。"""
    await kill_tree(process)


@dataclass
class ScriptRun:
    """一次脚本运行的结果。"""

    text: str  # 给模型看的结果文本，run_script 返回的就是它
    ok: bool  # 退出码为 0，且没有超时、没有异常
    stdout: str = ""  # 未截断的 stdout；超时 / 异常时为空


async def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    max_output_chars: int,
    script_label: str,
    stdin_text: str | None = None,
) -> str:
    """跑一个脚本子进程并把结果格式化成工具返回文本。"""
    run = await _run_script_process(
        cmd,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        script_label=script_label,
        stdin_text=stdin_text,
    )
    return run.text


async def _run_script_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    max_output_chars: int,
    script_label: str,
    stdin_text: str | None = None,
) -> ScriptRun:
    """跑一个脚本子进程，结果文本连同成败一起返回。会话脚本、Skill 脚本与 Skill
    声明的原生工具共用，避免几处重复维护超时/截断/日志逻辑。

    :param stdin_text: 给了就写进子进程 stdin（Skill 原生工具用它传参数 JSON）
    """
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **spawn_kwargs(),
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None),
            timeout=timeout_seconds,
        )
        raw_output = stdout.decode("utf-8", errors="replace")
        output = raw_output[:max_output_chars]
        if len(raw_output) > max_output_chars:
            # 静默硬切最坑：JSON 从中间断开，模型收到一段坏掉的结构还看不出被切过，
            # 只会照着残缺数据作答或反复重查。至少把「被切了、切掉多少」说出来。
            logger.warning(
                "run_script_output_truncated",
                script=script_label,
                total_chars=len(raw_output),
                kept_chars=max_output_chars,
            )
            output += (
                f"\n\n[输出已截断：共 {len(raw_output)} 字符，仅保留前 {max_output_chars} 字符，"
                "末尾是从中间切断的、不是完整结束。不要当成完整结果解析或据此作答"
                "（JSON 等结构化输出被切后无法解析）；请缩小查询范围，"
                "或让脚本只输出需要的字段后重跑。]"
            )
        if process.returncode != 0:
            # 报错放最前面：以前 stdout 在前、traceback 在后，stdout 一长被外置，模型得再
            # read_artifact 一次才看得到报错；traceback 还先摆出 5 层 runner / runpy 栈帧，
            # 真正的异常在最后（见 2026-10-22 审计）。多数 skill 脚本把结构化错误打到
            # stdout（JSON ok:false），所以 stdout 也照样带上。
            err = _clean_traceback(stderr.decode("utf-8", errors="replace")).strip()
            reason = _exception_line(err)
            parts = [f"脚本执行失败 (exit={process.returncode})" + (f"：{reason}" if reason else ":")]
            if err:
                if len(err) > _STDERR_TAIL_CHARS:
                    err = "…（前面的栈省略）\n" + err[-_STDERR_TAIL_CHARS:]
                parts.append(err)
            if output:
                if err:
                    parts.append("--- 出错前脚本的输出 ---")
                parts.append(output)
            if len(parts) == 1:
                parts.append("(无 stdout/stderr 输出)")
            return ScriptRun("\n".join(parts), ok=False, stdout=raw_output)
        return ScriptRun(output or "(脚本执行完毕，无输出)", ok=True, stdout=raw_output)
    except asyncio.TimeoutError:
        await _kill_process(process)
        return ScriptRun(f"错误: 脚本执行超时（>{timeout_seconds}s）", ok=False)
    except asyncio.CancelledError:
        # 用户停止 / 外层兜底超时：先清掉子进程再把取消传出去，否则留下孤儿进程
        await _kill_process(process)
        raise
    except Exception as exc:  # noqa: BLE001
        await _kill_process(process)
        logger.warning("run_script_failed", script=script_label, error=str(exc))
        return ScriptRun(f"脚本执行异常: {exc}", ok=False)


def _resolve_under_dir(
    base_dir: Path,
    relative: str,
    *,
    allow_absolute: bool = False,
) -> Path | str:
    """解析相对 base_dir 的路径；禁止越界。失败返回错误文案。"""
    raw = Path(relative)
    if raw.is_absolute():
        if not allow_absolute:
            return "错误: 默认不允许绝对路径；可在 tool_packs.yaml 设 allow_absolute: true"
        return raw.resolve()
    candidate = (base_dir / relative).resolve()
    try:
        candidate.relative_to(base_dir.resolve())
    except ValueError:
        return f"错误: 路径越出 scripts_dir={base_dir}"
    return candidate


def _default_shell_name() -> str:
    return "powershell" if sys.platform.startswith("win") else "bash"


class WriteScriptTool(BaseTool):
    """将内容写入 scripts_dir 下的脚本文件。"""

    name = "write_script"
    description = (
        "写入脚本文件。参数: path(相对 scripts 目录，默认落到 {session}/ 子目录), "
        "content(文件内容)；可覆盖已有文件，自动创建子目录。"
        "脚本以项目根为工作目录运行；定位文件请读环境变量 CSAGENT_PROJECT_ROOT / "
        "CSAGENT_SESSION_DIR / CSAGENT_OUTBOUND_DIR / CSAGENT_SCRIPT_DIR / "
        "CSAGENT_SKILLS_ROOTS，不要写死绝对路径。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对 scripts 目录的路径，如 demo.py",
                },
                "script": {
                    "type": "string",
                    "description": "path 的别名",
                },
                "content": {"type": "string", "description": "脚本全文"},
            },
            "required": ["content"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        scripts_dir: Path,
        allowed_extensions: list[str],
        allow_absolute: bool = False,
        session_scoped: bool = True,
    ) -> None:
        self._scripts_dir = scripts_dir
        self._allowed_extensions = set(allowed_extensions)
        self._allow_absolute = allow_absolute
        self._session_scoped = session_scoped

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        path_arg = str(arguments.get("path") or arguments.get("script") or "").strip()
        content = arguments.get("content")
        if content is None:
            return "错误: 缺少参数 content"
        content_str = content if isinstance(content, str) else str(content)
        if not content_str.strip():
            return "错误: content 不能为空"
        if not path_arg:
            return "错误: 缺少参数 path（相对 scripts 目录的路径，如 demo.py）"

        if self._session_scoped and not Path(path_arg).is_absolute():
            path_arg = bind_session_script_path(path_arg, scope.session_id)
        if err := _reject_dotdot(path_arg):
            return err

        target = _resolve_under_dir(
            self._scripts_dir,
            path_arg,
            allow_absolute=self._allow_absolute,
        )
        if isinstance(target, str):
            return target

        if self._allowed_extensions and target.suffix not in self._allowed_extensions:
            return (
                f"错误: 不支持的扩展名 {target.suffix}，"
                f"allowed={sorted(self._allowed_extensions)}"
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content_str.encode("utf-8")
            target.write_bytes(data)
            rel = (
                target.relative_to(self._scripts_dir.resolve()).as_posix()
                if not Path(path_arg).is_absolute()
                else str(target)
            )
            from csagent.audit.evidence import record_bytes

            record_bytes("write_script", data, path=rel)
            return f"已写入脚本: {rel}（{len(data)} bytes）"
        except Exception as exc:  # noqa: BLE001
            logger.warning("write_script_failed", path=path_arg, error=str(exc))
            return f"写入脚本失败: {exc}"


class ScriptTool(BaseTool):
    """执行脚本。

    参数：
    - script: 相对 scripts_dir 的路径（如 echo_demo.py 或 utils/foo.py）；
      也可在 allow_absolute=true 时传绝对路径
    - args: 可选字符串参数列表
    - action: 可选，传 list 时列出可用脚本

    会话脚本的 cwd 是 work_dir（默认项目根），与 run_shell 一致——两者对同一条
    相对路径必须给出同一个结果，否则模型会用 run_shell 验证通过、再用 run_script
    执行失败，只能靠写死绝对路径收场（见 audit wecom_BoWen）。
    Skill 自带脚本仍在自己的目录里跑：它们由 Skill 作者维护、用 __file__ 自定位，
    属于 Skill 的内部约定。
    """

    name = "run_script"
    description = (
        "执行脚本。参数: script(默认相对当前会话 scripts/{session}/；"
        "也可直接写共享脚本名), args(可选列表)；action=list 列出可用脚本。"
        "脚本工作目录为项目根；用环境变量 CSAGENT_PROJECT_ROOT / CSAGENT_SESSION_DIR / "
        "CSAGENT_OUTBOUND_DIR / CSAGENT_SCRIPT_DIR / CSAGENT_SKILLS_ROOTS 定位文件，"
        "不要在脚本里写死绝对路径。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "脚本相对路径或名称",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令行参数列表",
                },
                "action": {
                    "type": "string",
                    "description": "传 list 时列出可用脚本",
                },
            },
            "additionalProperties": False,
        }

    def __init__(
        self,
        scripts_dir: Path,
        allowed_extensions: list[str],
        timeout_seconds: int = 60,
        sandbox: bool = False,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        allow_absolute: bool = False,
        session_scoped: bool = True,
        env_passthrough: list[str] | None = None,
        env_extra: dict[str, str] | None = None,
        work_dir: Path | None = None,
        *,
        env_extra_all: dict[str, str] | None = None,
        skill_scripts_only: bool = False,
    ) -> None:
        self._scripts_dir = scripts_dir
        self._allowed_extensions = set(allowed_extensions)
        self._timeout_seconds = timeout_seconds
        self._sandbox = sandbox
        # 默认 30000，与外置门槛对齐；YAML 可覆盖。
        self._max_output_chars = max_output_chars
        self._allow_absolute = allow_absolute
        self._session_scoped = session_scoped
        self._env_passthrough = env_passthrough
        # env_extra 只给 Skill 自带脚本（代码由 Skill 作者维护）；env_extra_all 给所有脚本，
        # 包括模型现写的会话脚本，只该放路径、开关这类非敏感值
        self._env_extra = env_extra or {}
        self._env_extra_all = env_extra_all or {}
        # True：只允许运行 <skill:name>/scripts/ 下的脚本，会话脚本与共享脚本一律拒绝
        self._skill_scripts_only = skill_scripts_only
        self._work_dir = (work_dir or PROJECT_ROOT).resolve()

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        """跑脚本；没跑成（参数不对、脚本不存在、退出码非 0、超时）抛 ToolCallError，
        文本不变，审计里记 error——以前一律返回字符串，失败全记成 success。"""
        run = await self._execute(arguments, scope)
        if not run.ok:
            raise ToolCallError(run.text)
        return run.text

    async def _execute(self, arguments: dict[str, Any], scope: CustomerScope) -> ScriptRun:
        action = str(arguments.get("action") or "").strip().lower()
        if action == "list":
            if self._skill_scripts_only:
                return ScriptRun(_SKILL_SCRIPTS_ONLY_HINT, ok=True)
            return ScriptRun(self._format_script_list(scope.session_id), ok=True)

        script_name = str(arguments.get("script") or "").strip()
        script_args = arguments.get("args") or []
        if not isinstance(script_args, list):
            script_args = [script_args]
        script_args = [str(a) for a in script_args]

        if not script_name:
            if self._skill_scripts_only:
                return ScriptRun(f"错误: 缺少参数 script。{_SKILL_SCRIPTS_ONLY_HINT}", ok=False)
            return ScriptRun(
                "错误: 缺少参数 script。"
                f"可用脚本: {self._script_names(scope.session_id) or '（无）'}。"
                "或传 action=list 查看列表。",
                ok=False,
            )

        if is_skill_path(script_name):
            return await self._execute_skill_script(script_name, script_args, scope)
        if self._skill_scripts_only:
            logger.warning("run_script_non_skill_rejected", script=script_name[:200])
            return ScriptRun(f"错误: 拒绝执行 {script_name}。{_SKILL_SCRIPTS_ONLY_HINT}", ok=False)

        script_path = self._resolve_script(script_name, scope.session_id)
        if isinstance(script_path, str):
            return ScriptRun(script_path, ok=False)

        if not script_path.is_file():
            return ScriptRun(
                f"错误: 脚本 {script_name} 不存在。"
                f"可用: {self._script_names(scope.session_id) or '（无）'}",
                ok=False,
            )
        if self._allowed_extensions and script_path.suffix not in self._allowed_extensions:
            return ScriptRun(
                f"错误: 不支持的扩展名 {script_path.suffix}，"
                f"allowed={sorted(self._allowed_extensions)}",
                ok=False,
            )

        return await self._launch_script(
            script_path,
            script_args,
            scope,
            cwd=self._work_dir,
            script_label=script_name,
            skill_script=False,
        )

    async def _execute_skill_script(
        self, script_name: str, script_args: list[str], scope: CustomerScope
    ) -> ScriptRun:
        """执行 <skill:name>/scripts/x.py：只读解析到 Skill 目录，绕开 scripts_dir/session 逻辑。"""
        script_path = self._resolve_skill_script(script_name)
        if isinstance(script_path, str):
            return ScriptRun(script_path, ok=False)
        return await self._launch_script(
            script_path,
            script_args,
            scope,
            cwd=script_path.parent,
            script_label=script_name,
            skill_script=True,
        )

    async def run_skill_command(
        self,
        script_name: str,
        script_args: list[str],
        scope: CustomerScope,
        *,
        stdin_text: str,
    ) -> ScriptRun:
        """供 Skill 声明的原生工具调用：按 Skill 脚本的口径（环境变量、超时、输出上限）运行，
        参数经 stdin 传入，结果连同成败一起返回。"""
        script_path = self._resolve_skill_script(script_name)
        if isinstance(script_path, str):
            return ScriptRun(script_path, ok=False)
        return await self._launch_script(
            script_path,
            script_args,
            scope,
            cwd=script_path.parent,
            script_label=script_name,
            skill_script=True,
            stdin_text=stdin_text,
        )

    def _resolve_skill_script(self, script_name: str) -> Path | str:
        script_path = resolve_skill_resource(script_name)
        if isinstance(script_path, str):
            return script_path
        if not script_path.is_file():
            return f"错误: 脚本 {script_name} 不存在"
        if self._allowed_extensions and script_path.suffix not in self._allowed_extensions:
            return (
                f"错误: 不支持的扩展名 {script_path.suffix}，"
                f"allowed={sorted(self._allowed_extensions)}"
            )
        return script_path

    async def _launch_script(
        self,
        script_path: Path,
        script_args: list[str],
        scope: CustomerScope,
        *,
        cwd: Path,
        script_label: str,
        skill_script: bool,
        stdin_text: str | None = None,
    ) -> ScriptRun:
        job_file: Path | None = None
        try:
            from csagent.audit.evidence import record_path

            record_path("run_script", script_path, copy=True, script=script_label)
            cmd, job_file = self._build_command(script_path, script_args)
            if isinstance(cmd, str):
                return ScriptRun(cmd, ok=False)
            extra = (
                {**self._env_extra_all, **self._env_extra}
                if skill_script
                else dict(self._env_extra_all)
            )
            env = _build_scope_env(
                scope,
                sandbox=self._sandbox,
                env_passthrough=self._env_passthrough,
                env_extra=extra,
                project_root=self._work_dir,
            )
            _prepend_pythonpath(env, skill_python_paths())
            return await _run_script_process(
                cmd,
                cwd=cwd,
                env=env,
                timeout_seconds=self._timeout_seconds,
                max_output_chars=self._max_output_chars,
                script_label=script_label,
                stdin_text=stdin_text,
            )
        finally:
            _unlink_quietly(job_file)

    def _resolve_script(self, script_name: str, session_id: str) -> Path | str:
        if Path(script_name).is_absolute():
            return _resolve_under_dir(
                self._scripts_dir, script_name, allow_absolute=self._allow_absolute
            )

        candidates: list[str] = []
        if self._session_scoped:
            candidates.append(bind_session_script_path(script_name, session_id))
            # 共享脚本：scripts_dir 根下的单文件名（如 echo_demo.py）
            normalized = script_name.replace("\\", "/").strip()
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if (
                normalized
                and "/" not in normalized
                and ".." not in Path(normalized).parts
            ):
                candidates.append(normalized)
        else:
            candidates.append(script_name)

        last_err: str | None = None
        for cand in candidates:
            if err := _reject_dotdot(cand):
                last_err = err
                continue
            resolved = _resolve_under_dir(
                self._scripts_dir, cand, allow_absolute=self._allow_absolute
            )
            if isinstance(resolved, str):
                last_err = resolved
                continue
            if resolved.is_file():
                return resolved
            last_err = None
        if last_err:
            return last_err
        # 返回会话路径供上层报「不存在」
        bound = (
            bind_session_script_path(script_name, session_id)
            if self._session_scoped
            else script_name
        )
        if err := _reject_dotdot(bound):
            return err
        return _resolve_under_dir(
            self._scripts_dir, bound, allow_absolute=self._allow_absolute
        )

    def _script_names(self, session_id: str = "") -> list[str]:
        if not self._scripts_dir.is_dir():
            return []
        names: list[str] = []
        session_prefix = (
            f"{safe_session_segment(session_id)}/"
            if self._session_scoped and session_id
            else ""
        )
        for path in sorted(self._scripts_dir.rglob("*")):
            if not path.is_file():
                continue
            if self._allowed_extensions and path.suffix not in self._allowed_extensions:
                continue
            rel = path.relative_to(self._scripts_dir).as_posix()
            if self._session_scoped and session_id:
                # 当前会话脚本 + 根目录共享脚本
                if rel.startswith(session_prefix) or "/" not in rel:
                    names.append(rel)
            else:
                names.append(rel)
        return names

    def _format_script_list(self, session_id: str = "") -> str:
        names = self._script_names(session_id)
        if not names:
            hint = ""
            if self._session_scoped and session_id:
                hint = f"（会话子目录: {safe_session_segment(session_id)}/）"
            return f"目录 {self._scripts_dir}{hint} 下暂无可用脚本。"
        lines = [f"可用脚本（目录: {self._scripts_dir}）:"]
        lines.extend(f"- {n}" for n in names)
        return "\n".join(lines)

    def _build_command(
        self, script_path: Path, script_args: list[str]
    ) -> tuple[list[str] | str, Path | None]:
        if script_path.suffix == ".py":
            if not _ARGV_RUNNER.is_file():
                return f"错误: 找不到 argv runner {_ARGV_RUNNER}", None
            job_file = _write_argv_job(script_path, script_args)
            # 命令行只传 runner + job 路径；带引号的 --payload 不进 CreateProcess
            return [sys.executable, str(_ARGV_RUNNER), str(job_file)], job_file
        if script_path.suffix == ".sh":
            shell = shutil.which("bash") or shutil.which("sh")
            if not shell:
                return (
                    "错误: 未找到 bash/sh，无法执行 .sh；"
                    "请安装 Git Bash / WSL，或改用 .py 脚本。"
                ), None
            return [shell, str(script_path), *script_args], None
        if script_path.suffix in self._allowed_extensions or not self._allowed_extensions:
            return [str(script_path), *script_args], None
        return f"错误: 不支持的脚本类型 {script_path.suffix}", None


class ShellTool(BaseTool):
    """执行一条 shell 命令（开发机宽松：工作目录 + 超时）。"""

    name = "run_shell"
    description = (
        "执行 shell 命令。参数: command(必填), shell(可选: powershell|cmd|bash|sh)。"
        "默认 cwd 为项目根；超时后杀进程。可用 python -c 跑一行代码。"
    )

    # ---------- 危险命令拦截清单 ----------
    # 匹配到的命令会被直接拦截并返回错误，防止误操作破坏环境
    _DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        # 文件系统破坏
        (re.compile(r"\brm\s+(-[^\s]*\s+)*-[^\s]*r", re.IGNORECASE), "递归删除（rm -r）"),
        (re.compile(r"\brm\s+(-[^\s]*\s+)*-[^\s]*f", re.IGNORECASE), "强制删除（rm -f）"),
        (re.compile(r"\brmdir\s+/s", re.IGNORECASE), "递归删除目录（rmdir /s）"),
        (re.compile(r"\bdel\s+/[sfq]", re.IGNORECASE), "批量删除（del /s|/f|/q）"),
        (re.compile(r"Remove-Item\s.*-Recurse", re.IGNORECASE), "递归删除（Remove-Item -Recurse）"),
        # 磁盘格式化
        (re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE), "磁盘格式化"),
        (re.compile(r"\bmkfs\b", re.IGNORECASE), "文件系统格式化"),
        # 权限/所有权篡改
        (re.compile(r"\bchmod\s+(-[^\s]+\s+)*777\b"), "开放所有权限（chmod 777）"),
        (re.compile(r"\bchown\s+(-[^\s]+\s+)*root\b"), "变更所有者为 root"),
        # 系统关机/重启
        (re.compile(r"\bshutdown\b", re.IGNORECASE), "系统关机"),
        (re.compile(r"\breboot\b", re.IGNORECASE), "系统重启"),
        (re.compile(r"Stop-Computer\b", re.IGNORECASE), "系统关机（PowerShell）"),
        (re.compile(r"Restart-Computer\b", re.IGNORECASE), "系统重启（PowerShell）"),
        # 注册表/系统配置破坏
        (re.compile(r"\breg\s+delete\b", re.IGNORECASE), "删除注册表"),
        (re.compile(r"Remove-ItemProperty.*Registry", re.IGNORECASE), "删除注册表项"),
        # 网络渗透 / 提权
        (re.compile(r"\bnc\s+-[^\s]*l", re.IGNORECASE), "监听端口（netcat）"),
        (re.compile(r"\bsudo\s+su\b"), "切换 root"),
        # 环境变量篡改（覆盖 PATH）
        (re.compile(r"\bexport\s+PATH\s*=\s*['\"]?/"), "覆盖 PATH"),
        (re.compile(r"\$env:PATH\s*=\s*['\"]", re.IGNORECASE), "覆盖 PATH（PowerShell）"),
        # curl/wget 管道执行（远程代码执行）
        (re.compile(r"(curl|wget)\s[^|]*\|\s*(bash|sh|python|powershell)", re.IGNORECASE),
         "远程代码管道执行"),
        # dd 覆写
        (re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE), "dd 覆写设备"),
        # fork bomb
        (re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"), "fork bomb"),
    ]

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令行"},
                "shell": {
                    "type": "string",
                    "description": "powershell | cmd | bash | sh",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        work_dir: Path,
        timeout_seconds: int = 60,
        sandbox: bool = False,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        default_shell: str = "powershell",
        danger_guard: bool = True,
        env_passthrough: list[str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self._work_dir = work_dir
        self._timeout_seconds = timeout_seconds
        self._sandbox = sandbox
        # 默认 30000，与外置门槛对齐；YAML 可覆盖。
        self._max_output_chars = max_output_chars
        self._default_shell = (default_shell or _default_shell_name()).strip().lower()
        self._danger_guard = danger_guard
        self._env_passthrough = env_passthrough
        self._env_extra = env_extra or {}

    def check_dangerous(self, command: str) -> str | None:
        """检查命令是否匹配危险模式。匹配则返回拒绝原因，否则返回 None。"""
        if not self._danger_guard:
            return None
        for pattern, reason in self._DANGEROUS_PATTERNS:
            if pattern.search(command):
                return reason
        return None

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return "错误: 缺少参数 command"

        # 危险命令拦截
        danger_reason = self.check_dangerous(command)
        if danger_reason:
            logger.warning(
                "shell_danger_blocked",
                command=command[:200],
                reason=danger_reason,
            )
            return (
                f"命令被安全策略拦截: {danger_reason}。\n"
                f"如确需执行，请通过管理员审批或手动操作。"
            )

        shell_name = str(arguments.get("shell") or self._default_shell).strip().lower()
        from csagent.audit.evidence import record_bytes

        record_bytes("run_shell", command.encode("utf-8"), shell=shell_name)
        cmd = self._build_shell_command(shell_name, command)
        if isinstance(cmd, str):
            return cmd

        env = _build_scope_env(
            scope,
            sandbox=self._sandbox,
            env_passthrough=self._env_passthrough,
            env_extra=self._env_extra,
            project_root=self._work_dir,
        )
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._work_dir),
                env=env,
                **spawn_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
            out = stdout.decode("utf-8", errors="replace")[: self._max_output_chars]
            err = stderr.decode("utf-8", errors="replace")[:2000]
            if process.returncode != 0:
                parts = [f"命令失败 (exit={process.returncode}, shell={shell_name}):"]
                if out:
                    parts.append(out)
                if err:
                    parts.append(err)
                return "\n".join(parts)
            if out:
                return out
            if err:
                return err
            return "(命令执行完毕，无输出)"
        except asyncio.TimeoutError:
            await _kill_process(process)
            return f"命令执行超时 ({self._timeout_seconds}s)"
        except asyncio.CancelledError:
            await _kill_process(process)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("shell_tool_failed", shell=shell_name, error=str(exc))
            return f"命令执行异常: {exc}"

    def _build_shell_command(self, shell_name: str, command: str) -> list[str] | str:
        if shell_name == "powershell":
            exe = (
                shutil.which("powershell")
                or shutil.which("pwsh")
                or shutil.which("powershell.exe")
            )
            if not exe:
                return "错误: 未找到 powershell/pwsh"
            return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
        if shell_name == "cmd":
            exe = shutil.which("cmd") or shutil.which("cmd.exe")
            if not exe:
                return "错误: 未找到 cmd"
            return [exe, "/c", command]
        if shell_name in {"bash", "sh"}:
            exe = shutil.which(shell_name) or shutil.which("bash") or shutil.which("sh")
            if not exe:
                return f"错误: 未找到 {shell_name}；请安装 Git Bash / WSL"
            return [exe, "-lc", command]
        return (
            f"错误: 不支持的 shell={shell_name!r}，"
            "请使用 powershell / cmd / bash / sh。"
        )


class ScriptToolPack(ToolPack):
    """写脚本 / 跑脚本 / 执行 shell 工具包。"""

    pack_name = "script_executor"
    description = "写脚本 / 跑脚本 / 执行 shell 工具包"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []
        self.scripts_dir: Path | None = None
        self.work_dir: Path | None = None

    def initialize(self, config: dict[str, Any]) -> None:
        scripts_dir = Path(str(config.get("scripts_dir") or "scripts/tools"))
        if not scripts_dir.is_absolute():
            scripts_dir = PROJECT_ROOT / scripts_dir
        scripts_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir = scripts_dir

        work_dir = Path(str(config.get("work_dir") or "."))
        if not work_dir.is_absolute():
            work_dir = PROJECT_ROOT / work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = work_dir

        extensions = [str(e) for e in (config.get("allowed_extensions") or [".sh", ".py"])]
        timeout_seconds = int(config.get("timeout_seconds") or 60)
        sandbox = bool(config.get("sandbox", False))
        max_output_chars = int(config.get("max_output_chars") or DEFAULT_MAX_OUTPUT_CHARS)
        allow_absolute = bool(config.get("allow_absolute", False))
        session_scoped = bool(config.get("session_scoped", True))
        write_enabled = bool(config.get("write_enabled", True))
        shell_enabled = bool(config.get("shell_enabled", True))
        default_shell = str(config.get("default_shell") or "").strip() or _default_shell_name()
        # YAML 常写 powershell；非 Windows 自动回退 bash
        if default_shell == "powershell" and not sys.platform.startswith("win"):
            default_shell = "bash"
        # None=不配置该 key，_build_scope_env 全量继承 os.environ（现状）；
        # 显式给列表（含空列表）才切到白名单模式——不能用 `or []` 抹掉这个区别
        env_passthrough_raw = config.get("env_passthrough")
        env_passthrough = (
            [str(x) for x in env_passthrough_raw] if env_passthrough_raw is not None else None
        )
        # env_extra / env_extra_all：YAML 只写变量名，值从 .env / os.environ 解析
        # （见 resolve_env_extra）。env_extra 只给 Skill 自带脚本，凭证类变量只能放这里；
        # env_extra_all 连模型现写的脚本也看得到，只放路径、开关这类非敏感值。
        env_extra = resolve_env_extra(config.get("env_extra"), project_root=work_dir)
        env_extra_all = resolve_env_extra(config.get("env_extra_all"), project_root=work_dir)
        skill_scripts_only = bool(config.get("skill_scripts_only", False))

        tools: list[BaseTool] = []
        if write_enabled:
            tools.append(
                WriteScriptTool(
                    scripts_dir=scripts_dir,
                    allowed_extensions=extensions,
                    allow_absolute=allow_absolute,
                    session_scoped=session_scoped,
                )
            )
        run_tool = ScriptTool(
            scripts_dir=scripts_dir,
            allowed_extensions=extensions,
            timeout_seconds=timeout_seconds,
            sandbox=sandbox,
            max_output_chars=max_output_chars,
            allow_absolute=allow_absolute,
            session_scoped=session_scoped,
            env_passthrough=env_passthrough,
            env_extra=env_extra,
            work_dir=work_dir,
            env_extra_all=env_extra_all,
            skill_scripts_only=skill_scripts_only,
        )
        tools.append(run_tool)
        if shell_enabled:
            danger_guard = bool(config.get("danger_guard", True))
            tools.append(
                ShellTool(
                    work_dir=work_dir,
                    timeout_seconds=timeout_seconds,
                    sandbox=sandbox,
                    max_output_chars=max_output_chars,
                    default_shell=default_shell,
                    danger_guard=danger_guard,
                    env_passthrough=env_passthrough,
                    # shell 命令是模型现写的，与会话脚本同口径，拿不到 Skill 专用变量
                    env_extra=env_extra_all,
                )
            )

        desc_by_name = {
            str(item.get("name")): str(item.get("description") or "")
            for item in (config.get("tools") or [])
            if isinstance(item, dict) and item.get("name")
        }
        for tool in tools:
            if desc_by_name.get(tool.name):
                tool.description = desc_by_name[tool.name]
        if skill_scripts_only:
            run_tool.description = f"{run_tool.description}（{_SKILL_SCRIPTS_ONLY_HINT}）"

        self._tools = tools
        logger.info(
            "script_pack_ready",
            scripts_dir=str(scripts_dir),
            work_dir=str(work_dir),
            tools=[t.name for t in tools],
            available=run_tool._script_names(),
            allow_absolute=allow_absolute,
            session_scoped=session_scoped,
            write_enabled=write_enabled,
            shell_enabled=shell_enabled,
            skill_scripts_only=skill_scripts_only,
            default_shell=default_shell,
            env_passthrough=env_passthrough,
            env_extra=sorted(env_extra),
            env_extra_all=sorted(env_extra_all),
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
