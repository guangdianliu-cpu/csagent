"""基金产品中心查询脚本：fund-product-query 这个 Skill 的业务入口。

四个 action 由 Skill 的 tools.yaml 声明成原生工具（product_name_lookup / product_lookup /
product_entity_query / product_siblings）：内核把工具参数 JSON 经 stdin 交给 ``--payload -``，
业务逻辑仍全部留在 Skill 里，内核不认识任何产品字段。

也可以直接跑（调试用）：
    python query.py --action query|name_lookup|multi_lookup|share_siblings --payload '<JSON字符串>'

出口契约：成功原样输出（JSON，或 query 的「查询成功…」表格文本），exit 0；
失败统一成 {"ok": false, "error": {"code", "message"}}，exit 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import os
from pathlib import Path

# 本脚本物理上不在 src/csagent 包内（活在 skills/ 目录下），正常情况下
# csagent 已通过 editable install 对同一个解释器可见；这里做一个兜底，
# 避免部署环境的 editable install 状态不一致时直接 ImportError。
try:
    import csagent  # noqa: F401
except ImportError:
    _src_dir = Path(__file__).resolve().parents[3] / "src"
    if _src_dir.is_dir():
        sys.path.insert(0, str(_src_dir))

# 兜底：直接用 importlib（如 pytest）加载本脚本时，脚本目录不会像 `python query.py`
# 那样自动进 sys.path[0]，手动补上，让下面几个同目录的基金业务模块能被 import 到。
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import structlog  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# 子进程不会走 csagent.infra.logging.configure_logging()，structlog 默认的
# PrintLoggerFactory 直接往 stdout 写——后端 500 时 debug 日志会糊在 JSON 前面
# （2026-08-25 本地 rec 9 复现），模型拿到的就不是纯 JSON 了。
# 统一改写 stderr：stdout 只留结果，run_script 仅在非零退出时才把 stderr 带出来。
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

from csagent.infra.config import PROJECT_ROOT  # noqa: E402
from csagent.react.models import CustomerScope  # noqa: E402
from csagent.tools.semantic.catalog import QueryCatalog  # noqa: E402
from csagent.tools.semantic.executor import SemanticQueryExecutor  # noqa: E402

from multi_fund_tool import run_multi_fund_lookup  # noqa: E402
from name_lookup_tool import run_name_lookup  # noqa: E402
from product_center_backend import ProductCenterQueryBackend  # noqa: E402
from product_center_client import ProductCenterClient  # noqa: E402
from share_siblings_tool import run_share_siblings  # noqa: E402

_ACTIONS = ("query", "name_lookup", "multi_lookup", "share_siblings")
_ENTITIES_YAML = Path(__file__).resolve().parent / "entities.yaml"

# 失败文案 → 错误码。各业务模块与校验器的失败都是中文纯文本，这里按开头归类，
# 文案原样放进 message（允许值列表就在里面）。按顺序匹配，先具体后笼统。
_FAILURE_CODES = (
    (re.compile(r"^(参数错误|QuerySpec 格式错误)"), "BAD_PAYLOAD"),
    (re.compile(r"^产品中心查询失败: [a-z_]+ 需要"), "BAD_PAYLOAD"),
    (re.compile(r"^(权限错误: 不允许实体|未知实体|实体 \S+ 暂未接入)|未实现实体路由"), "UNKNOWN_ENTITY"),
    (re.compile(r"^字段 '[^']*' 不存在"), "FIELD_NOT_FOUND"),
    (re.compile(r"不可筛选"), "FIELD_NOT_FILTERABLE"),
    (re.compile(r"不可排序"), "FIELD_NOT_SORTABLE"),
    (re.compile(r"不支持操作符"), "OP_NOT_ALLOWED"),
    (re.compile(r"查询失败"), "UPSTREAM_ERROR"),
)

# 与原先 Settings(env_file=PROJECT_ROOT/.env) 一致：进程环境优先，缺省再读仓库 .env。
# 本脚本由 run_script 拉起子进程，父进程的 pydantic Settings 不会写入 os.environ。
load_dotenv(PROJECT_ROOT / ".env", override=False)


class _ArgError(Exception):
    """argparse 用法错误的可捕获形式（避免直接 sys.exit 触发 run_script 的异常退出包装）。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D102
        raise _ArgError(message)


def _build_parser() -> _Parser:
    parser = _Parser(description="基金产品中心查询脚本", add_help=False)
    parser.add_argument("--action", choices=_ACTIONS, required=True)
    parser.add_argument("--payload", required=True, help="JSON 对象字符串；写 - 表示从 stdin 读")
    return parser


def _build_client() -> ProductCenterClient:
    return ProductCenterClient(
        base_url=os.environ.get("PRODUCT_PC_BRIDGE_BASE_URL", "http://127.0.0.1:18080"),
        token=os.environ.get("PRODUCT_PC_BRIDGE_TOKEN", ""),
        timeout_seconds=float(os.environ.get("PRODUCT_PC_BRIDGE_TIMEOUT_SECONDS", "30")),
    )


async def _run_query(payload: dict) -> str:
    catalog = QueryCatalog.load(_ENTITIES_YAML)
    allowed = {e.name for e in catalog.entities_for_domain("product")}
    entity = payload.get("entity")
    if entity and entity not in allowed:
        allowed_text = ", ".join(sorted(allowed)) or "（无）"
        return f"权限错误: 不允许实体 '{entity}'。可用实体: {allowed_text}"

    client = _build_client()
    try:
        backend = ProductCenterQueryBackend(client)
        executor = SemanticQueryExecutor(catalog=catalog, backend=backend)
        outcome = await executor.execute_outcome(
            payload, CustomerScope(session_id="fund-product-query-script")
        )
        return outcome.text
    finally:
        await client.aclose()


async def _run_multi_lookup(payload: dict) -> str:
    client = _build_client()
    try:
        return await run_multi_fund_lookup(
            client,
            payload.get("fund_codes"),
            custtype=payload.get("custtype"),
            aspects=payload.get("aspects"),
            out_file=payload.get("out_file"),
        )
    finally:
        await client.aclose()


async def _run_name_lookup(payload: dict) -> str:
    client = _build_client()
    try:
        return await run_name_lookup(
            client,
            payload.get("fund_names"),
            limit=payload.get("limit"),
        )
    finally:
        await client.aclose()


async def _run_share_siblings(payload: dict) -> str:
    client = _build_client()
    try:
        return await run_share_siblings(
            client,
            payload.get("fund_code"),
            mode=os.environ.get("PRODUCT_SHARE_SIBLING_MODE", "consign_only"),
        )
    finally:
        await client.aclose()


async def _dispatch(action: str, payload: dict) -> str:
    if action == "query":
        return await _run_query(payload)
    if action == "name_lookup":
        return await _run_name_lookup(payload)
    if action == "multi_lookup":
        return await _run_multi_lookup(payload)
    return await _run_share_siblings(payload)


def _is_success(text: str) -> bool:
    if text.startswith("查询成功"):
        return True
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _emit(text: str) -> int:
    """成功原样输出，exit 0；失败包成统一信封，exit 1。

    以前失败也走 exit 0，Skill 原生工具与审计都分不出这次调用失败了。
    """
    stripped = (text or "").strip()
    if _is_success(stripped):
        print(stripped)
        return 0
    code = next((c for pattern, c in _FAILURE_CODES if pattern.search(stripped)), "QUERY_REJECTED")
    print(json.dumps({"ok": False, "error": {"code": code, "message": stripped}}, ensure_ascii=False))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgError as exc:
        return _emit(f"参数错误: {exc}")

    # 原生工具传 --payload -：参数 JSON 从 stdin 读，不经命令行转义
    raw = sys.stdin.read() if args.payload == "-" else args.payload
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _emit(f"参数错误: payload 不是合法 JSON: {exc}")
    if not isinstance(payload, dict):
        return _emit("参数错误: payload 必须是 JSON 对象")

    return _emit(asyncio.run(_dispatch(args.action, payload)))


if __name__ == "__main__":
    raise SystemExit(main())
